"""zatsumu server — テレワーク勤怠・稼働可視化 MVP (F-Chair+ 風)."""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from . import db, devices, imaging, mail, mail_events, notify, notion, reports, retention, tz

DATA_DIR = Path(os.environ.get("ZATSUMU_DATA_DIR", db.DB_PATH.parent))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
# 受信メールの添付ファイル (<メールの番号>/<添付の番号> に保存する)
MAIL_ATTACHMENT_DIR = DATA_DIR / "mail_attachments"
# バックグラウンド処理(キャプチャ自動削除・長時間在席チェック)の実行間隔(分)。
# メールの定期受信の間隔は、これとは別にメール画面の設定 (mail_fetch_interval_min) で決める
ALERT_CHECK_INTERVAL_MIN = float(os.environ.get("ZATSUMU_CHECK_INTERVAL_MIN", "10"))
# 起動してから最初にメールを受信するまでの秒数 (更新・再起動のたびに、受信の間隔いっぱい
# 新着が止まらないように。起動直後の混み合いを避けて少しだけ待つ)
MAIL_FIRST_FETCH_DELAY_SEC = 30
# 次の受信を待つ間に、受信の間隔 (設定) を読み直す間隔 (秒)。
# 画面で間隔を変えたら、再起動しなくても (長い間隔で待っている途中でも) すぐ使われる
MAIL_LOOP_RECHECK_SEC = 15
# 「今すぐ受信」を押したときに、ほかの受信 (定期受信など) が終わるのを待つ秒数
FETCH_WAIT_SEC = 15
# メールの自動受信の間隔 (分) に選べる範囲 (設定 mail_fetch_interval_min)
MAIL_FETCH_INTERVAL_RANGE = (1, 60)


def check_long_seated(conn) -> int:
    """alert_hours を超えて在席中のセッションを通知する (重複通知はしない)."""
    s = db.get_settings(conn)
    if not s["notify_alert"]:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=s["alert_hours"])
    rows = conn.execute(
        """
        SELECT se.id, se.clock_in, u.name, u.email FROM sessions se
        JOIN users u ON u.id = se.user_id
        WHERE se.clock_out IS NULL AND se.alert_notified = 0 AND se.clock_in <= ?
              AND u.notify_enabled = 1
        """,
        (tz.utc_iso(cutoff),),
    ).fetchall()
    for r in rows:
        notify.deliver_async(
            _with_member_email(s, r["email"]),
            f"⚠ {r['name']} さんが {s['alert_hours']} 時間以上連続で在席中です",
        )
        conn.execute(
            "UPDATE sessions SET alert_notified = 1 WHERE id = ?", (r["id"],)
        )
    conn.commit()
    return len(rows)


def check_clockout_reminders(conn) -> int:
    """終業時刻(clockout_reminder_time)を過ぎても未退勤の本人へ1回だけリマインド."""
    s = db.get_settings(conn)
    if not s["clockout_reminder"]:
        return 0
    now = datetime.now(timezone.utc)
    # ローカル時刻が設定時刻を過ぎているか ("HH:MM" のゼロ埋め文字列比較でOK)
    if now.astimezone(tz.TZ).strftime("%H:%M") < s["clockout_reminder_time"]:
        return 0
    day_start, _ = tz.today_window(now)
    rows = conn.execute(
        """
        SELECT se.id, u.name, u.email FROM sessions se
        JOIN users u ON u.id = se.user_id
        WHERE se.clock_out IS NULL AND se.clockout_reminded = 0
              AND se.clock_in >= ? AND u.notify_enabled = 1
        """,
        (tz.utc_iso(day_start),),
    ).fetchall()
    for r in rows:
        notify.deliver_async(
            _with_member_email(s, r["email"]),
            f"⏰ {r['name']} さん、退勤打刻がまだのようです。"
            f"終業の際は退勤ボタンを押してください。",
        )
        conn.execute(
            "UPDATE sessions SET clockout_reminded = 1 WHERE id = ?", (r["id"],)
        )
    conn.commit()
    return len(rows)


def _mail_tick() -> None:
    """メールの定期受信と保存期間パージ、返事をしていないメールのお知らせ、週ごとのまとめ
    (ネットワークI/Oを含むため別スレッドで実行)."""
    conn = db.connect(DATA_DIR / "zatsumu.db")
    try:
        try:
            mail.check_mail(conn, attachment_dir=MAIL_ATTACHMENT_DIR)
            mail.purge_old_mails(conn, db.get_settings(conn)["mail_retention_days"],
                                 MAIL_ATTACHMENT_DIR)
        finally:
            # 受信に失敗しても (受信サーバの不調など)、届いているメールのお知らせは続ける。
            # お知らせの失敗はここでログに出して止め、受信の失敗 (ループのログに出る) を隠さない
            try:
                conn.rollback()
                mail.remind_unanswered(conn)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                print(f"返事をしていないメールのお知らせでエラー: {e}")
            # 週ごとのまとめ (決めた曜日・時刻に1回だけ)。失敗してもほかの処理は止めない
            try:
                conn.rollback()
                mail.send_weekly_report(conn)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                print(f"週ごとのまとめでエラー: {e}")
    finally:
        conn.close()


async def _background_loop() -> None:
    while True:
        await asyncio.sleep(ALERT_CHECK_INTERVAL_MIN * 60)
        # 1 回の失敗(DB/IO/送信エラー等)でループ自体が死なないよう握りつぶす。
        # CancelledError は BaseException なので捕捉せず shutdown を妨げない。
        try:
            conn = db.connect(DATA_DIR / "zatsumu.db")
            try:
                days = db.get_settings(conn)["retention_days"]
                if days > 0:
                    retention.purge_old_screenshots(conn, SCREENSHOT_DIR, days)
                check_long_seated(conn)
                check_clockout_reminders(conn)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"バックグラウンド処理でエラー: {e}")


def _mail_interval_sec() -> float:
    """メールの定期受信の間隔 (秒)。設定 (メール画面) の値で、読めなければ既定の値."""
    lo, hi = MAIL_FETCH_INTERVAL_RANGE
    try:
        conn = db.connect(DATA_DIR / "zatsumu.db")
        try:
            minutes = db.get_settings(conn)["mail_fetch_interval_min"]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"メールの受信間隔を読めませんでした: {e}")
        minutes = db.INT_SETTINGS["mail_fetch_interval_min"]
    return min(max(minutes, lo), hi) * 60


async def _mail_loop() -> None:
    # メール受信は IMAP/Claude API で数分かかることがあるため、パージや
    # アラートを遅らせないよう独立ループにする。ブロッキングI/O (DB を開くのも含む) は
    # to_thread でイベントループの外に逃がす。
    await asyncio.sleep(MAIL_FIRST_FETCH_DELAY_SEC)
    while True:
        started = time.monotonic()
        try:
            await asyncio.to_thread(_mail_tick)
        except Exception as e:  # noqa: BLE001
            print(f"メール受信処理でエラー: {e}")
        # 前の受信を始めてから、間隔 (設定) が経つまで待つ。間隔は少しずつ読み直す
        while True:
            left = await asyncio.to_thread(_mail_interval_sec) - (time.monotonic() - started)
            if left <= 0:
                break
            await asyncio.sleep(min(left, MAIL_LOOP_RECHECK_SEC))


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(DATA_DIR / "zatsumu.db")
    try:
        if os.environ.get("ZATSUMU_DEMO") == "1":
            from . import demo

            if demo.seed(conn, SCREENSHOT_DIR, MAIL_ATTACHMENT_DIR):
                print("デモデータを投入しました (管理者トークン: demo-admin)")
        # 前からあるメールの「どのメールへの返事か」を覚える (返信画面の「この相手との
        # やり取り」用。前の回に調べ終えたメールは調べない)。失敗しても起動は止めない
        try:
            filled = mail.fill_missing_refs(conn)
            if filled:
                print(f"メール {filled} 通のつながり（返事の元）を記録しました")
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"メールのつながりを記録できませんでした: {e}")
        # 保存済みのタイムゾーン設定を適用する
        tz.set_tz(db.get_settings(conn)["timezone"])
    finally:
        conn.close()
    task = asyncio.create_task(_background_loop())
    mail_task = asyncio.create_task(_mail_loop())
    yield
    task.cancel()
    mail_task.cancel()


# 自動の API 説明ページ(/docs, /redoc, /openapi.json)は出さない。社内ツールで
# 外部に API を公開していないので不要で、出すと全 API の一覧(トークン再発行など
# 狙われやすい経路を含む)とその場で試せる画面を誰にでも見せることになる
app = FastAPI(
    title="zatsumu",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _no_search_index(request: Request, call_next):
    """社内ツールなので検索エンジンに載せない.

    公開サービス(例: 自社プロダクト)と同じドメイン配下や同じサーバに置かれても、
    ログイン画面が検索結果に出てブランドを損ねないようにする。
    robots.txt で Disallow すると noindex が読まれず逆効果なので、ヘッダで伝える。
    """
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    conn = db.connect(DATA_DIR / "zatsumu.db")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def token_user(authorization: str = Header(None), conn=Depends(get_conn)):
    """Bearer トークンだけで本人確認する (端末の確認はしない)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    user = db.user_by_token(conn, authorization.removeprefix("Bearer "))
    if not user:
        raise HTTPException(401, "Invalid token")
    return user


def auth_user(
    user=Depends(token_user),
    x_device_token: str = Header(None),
    conn=Depends(get_conn),
):
    # 新しい端末のメール確認 (ZATSUMU_LOGIN_VERIFY_EMAIL 設定時のみ)。
    # 管理者は確認済みの端末トークン (X-Device-Token) も必要。一般メンバーは対象外
    if user["is_admin"] and devices.enabled():
        if not devices.check_device(conn, user["id"], x_device_token):
            raise HTTPException(401, devices.REQUIRED_DETAIL)
        # check_device は使った時刻を書き込むことがある。ここで確定させないと、
        # その後の受信 (IMAP・AI で数分かかることがある) の間ずっと DB の書き込みロックを
        # 持ったままになり、ほかの人の打刻・設定の保存・返信の送信が失敗する
        conn.commit()
    return user


def require_admin(user=Depends(auth_user)):
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return user


def require_admin_token(user=Depends(token_user)):
    """管理者トークンだけを確かめる (端末の確認コードを受け取る API 用)."""
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return user


def _with_member_email(settings: dict, email: str | None) -> dict:
    """通知先(mail_to)にメンバー個別のメールアドレスを足した設定を返す."""
    if not email:
        return settings
    s = dict(settings)
    recips = [a.strip() for a in str(s.get("mail_to") or "").split(",") if a.strip()]
    if email not in recips:
        recips.append(email)
    s["mail_to"] = ",".join(recips)
    return s


def _notify(conn, text: str, member_email: str | None = None) -> None:
    """設定が有効なら通知を非同期送信する (失敗してもリクエストは止めない).

    member_email を渡すと、全社の通知先に加えて本人宛にも送る。
    """
    notify.deliver_async(
        _with_member_email(db.get_settings(conn), member_email), text
    )


def _audit(conn, admin, action, target_user_id=None, session_id=None, detail=""):
    """管理者操作の監査ログ (修正履歴CSVの元データ)."""
    conn.execute(
        "INSERT INTO audit_log (admin_id, action, target_user_id, session_id,"
        " detail, at) VALUES (?, ?, ?, ?, ?, ?)",
        (admin["id"], action, target_user_id, session_id, detail, now_iso()),
    )


def _parse_ts(value: str) -> str:
    """UI からの時刻 (datetime-local 等) を UTC ISO に正規化する.

    タイムゾーンが無い場合は ZATSUMU_TZ として解釈する。
    """
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"invalid datetime: {value}")
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz.TZ)
    return d.astimezone(timezone.utc).isoformat()


class UserCreate(BaseModel):
    name: str
    is_admin: bool = False


class UserPatch(BaseModel):
    active: bool | None = None
    is_admin: bool | None = None
    capture_enabled: bool | None = None
    notify_enabled: bool | None = None
    email: str | None = None
    team_id: int | None = None
    clear_team: bool = False  # team_id を未所属(NULL)に戻す


class TeamBody(BaseModel):
    name: str


class LeaveBody(BaseModel):
    date: str
    leave_type: str
    reason: str = ""


class CorrectionBody(BaseModel):
    date: str                       # YYYY-MM-DD
    requested_in: str | None = None   # "HH:MM" (任意)
    requested_out: str | None = None  # "HH:MM" (任意)
    reason: str = ""


class DecisionBody(BaseModel):
    approve: bool


class SettingsPatch(BaseModel):
    capture_min_interval: int | None = None
    capture_max_interval: int | None = None
    capture_quality: int | None = None
    capture_blur: int | None = None
    capture_enabled: bool | None = None
    retention_days: int | None = None
    alert_hours: int | None = None
    daily_target_minutes: int | None = None
    notify_clock: bool | None = None
    notify_alert: bool | None = None
    notify_journal: bool | None = None
    notify_stall: bool | None = None
    stall_threshold: int | None = None
    stall_alert_count: int | None = None
    clockout_reminder: bool | None = None
    clockout_reminder_time: str | None = None
    idle_threshold: int | None = None
    company_name: str | None = None
    timezone: str | None = None
    work_start: str | None = None
    work_end: str | None = None
    work_categories: str | None = None
    slack_webhook_url: str | None = None
    mail_to: str | None = None
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_user: str | None = None
    smtp_pass: str | None = None
    mail_from: str | None = None
    imap_host: str | None = None
    imap_port: str | None = None
    imap_user: str | None = None
    imap_pass: str | None = None
    imap_folder: str | None = None
    imap_sent_folder: str | None = None
    imap_use_ssl: bool | None = None
    mail_save_sent: bool | None = None
    mail_fetch_enabled: bool | None = None
    mail_fetch_interval_min: int | None = None
    mail_auto_draft: bool | None = None
    mail_retention_days: int | None = None
    mail_fetch_days: int | None = None
    notify_mail_high: bool | None = None
    mail_remind_enabled: bool | None = None
    mail_remind_hours_high: int | None = None
    mail_remind_hours_normal: int | None = None
    mail_remind_quiet: str | None = None
    mail_page_url: str | None = None
    mail_report_enabled: bool | None = None
    mail_report_weekday: int | None = None
    mail_report_hour: int | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None
    anthropic_classify_model: str | None = None
    mail_signature: str | None = None
    mail_reply_instructions: str | None = None
    mail_vip_addresses: str | None = None
    mail_urgent_keywords: str | None = None
    mail_categories: str | None = None
    mail_category_keywords: str | None = None
    mail_category_assignees: str | None = None
    notion_token: str | None = None
    notion_database_id: str | None = None
    notion_property_map: str | None = None
    notion_ai_fill: bool | None = None


class MailPatch(BaseModel):
    draft_reply: str | None = None
    priority: int | None = None
    status: str | None = None
    category: str | None = None           # 問い合わせの種類 ("" で未分類に戻す)
    assignee_user_id: int | None = None   # 担当の人 (null で担当なし)


class MailDraftBody(BaseModel):
    instructions: str = ""    # 返信の方針 (例: 「金曜までに対応すると伝える」)


class MailSendBody(BaseModel):
    body: str
    subject: str | None = None  # 省略時は「Re: 元の件名」
    cc: list[str] | None = None   # CC のアドレス (全員に返信。省略時は CC なし)
    bcc: list[str] | None = None  # BCC のアドレス (ヘッダには出ない)


class MailNotionBody(BaseModel):
    again: bool = False   # 登録済みでも、もう一度登録する (Notion に同じ問い合わせの行がもう1つできる)


class MailTemplateBody(BaseModel):
    name: str
    body: str


class MailTemplatePatch(BaseModel):
    name: str | None = None
    body: str | None = None
    move: str | None = None     # "up" / "down" (並び順を1つ上/下へ)


class SessionBody(BaseModel):
    clock_in: str
    clock_out: str
    category: str | None = None


class JournalBody(BaseModel):
    body: str
    date: str | None = None


def _valid_date(d: str | None) -> str:
    d = d or tz.today_str()
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date must be 'YYYY-MM-DD'")
    return d


def _valid_hhmm(t: str | None) -> str | None:
    """'HH:MM' を検証する (空/None は None を返す)."""
    if not t:
        return None
    try:
        datetime.strptime(t, "%H:%M")
    except ValueError:
        raise HTTPException(400, f"時刻は HH:MM 形式で入力してください: {t}")
    return t


class ClockInBody(BaseModel):
    category: str | None = None


class CategoryBody(BaseModel):
    category: str


def _resolve_category(conn, requested: str | None) -> str:
    cats = db.work_categories(conn)
    if requested is None:
        return cats[0]
    if requested not in cats:
        raise HTTPException(400, f"不明な作業区分です: {requested}")
    return requested


@app.get("/api/me")
def me(user=Depends(auth_user), conn=Depends(get_conn)):
    """自分の現在状態 (メンバー用打刻ページで使用)."""
    now = datetime.now(timezone.utc)
    day_start, _ = tz.today_window(now)
    open_s = db.open_session(conn, user["id"])
    sessions = conn.execute(
        "SELECT clock_in, clock_out FROM sessions WHERE user_id = ? "
        "AND (clock_out IS NULL OR clock_out > ?)",
        (user["id"], tz.utc_iso(day_start)),
    ).fetchall()
    hours = sum(
        tz.overlap_hours(s["clock_in"], s["clock_out"], day_start, now, now)
        for s in sessions
    )
    return {
        "name": user["name"],
        "seated": open_s is not None,
        "open_since": open_s["clock_in"] if open_s else None,
        "category": open_s["category"] if open_s else None,
        "hours_today": round(hours, 2),
        "categories": db.work_categories(conn),
    }


@app.post("/api/clock-in")
def clock_in(
    body: ClockInBody | None = None,
    user=Depends(auth_user),
    conn=Depends(get_conn),
):
    if db.open_session(conn, user["id"]):
        raise HTTPException(409, "Already clocked in")
    category = _resolve_category(conn, body.category if body else None)
    cur = conn.execute(
        "INSERT INTO sessions (user_id, clock_in, category) VALUES (?, ?, ?)",
        (user["id"], now_iso(), category),
    )
    if db.get_settings(conn)["notify_clock"] and user["notify_enabled"]:
        _notify(conn, f"🟢 {user['name']} さんが着席しました（{category}）",
                member_email=user["email"])
    return {"session_id": cur.lastrowid, "clock_in": now_iso(), "category": category}


@app.post("/api/switch-category")
def switch_category(
    body: CategoryBody, user=Depends(auth_user), conn=Depends(get_conn)
):
    """在席中に作業区分を切り替える (現在の在席を区切り、新区分で続行)."""
    session = db.open_session(conn, user["id"])
    if not session:
        raise HTTPException(409, "Not clocked in")
    category = _resolve_category(conn, body.category)
    if session["category"] == category:
        return {"category": category}  # 同じ区分なら何もしない
    ts = now_iso()
    conn.execute(
        "UPDATE sessions SET clock_out = ? WHERE id = ?", (ts, session["id"])
    )
    cur = conn.execute(
        "INSERT INTO sessions (user_id, clock_in, category) VALUES (?, ?, ?)",
        (user["id"], ts, category),
    )
    return {"session_id": cur.lastrowid, "category": category}


@app.post("/api/clock-out")
def clock_out(user=Depends(auth_user), conn=Depends(get_conn)):
    session = db.open_session(conn, user["id"])
    if not session:
        raise HTTPException(409, "Not clocked in")
    conn.execute(
        "UPDATE sessions SET clock_out = ? WHERE id = ?", (now_iso(), session["id"])
    )
    if db.get_settings(conn)["notify_clock"] and user["notify_enabled"]:
        now = datetime.now(timezone.utc)
        day_start, _ = tz.today_window(now)
        rows = conn.execute(
            "SELECT clock_in, clock_out FROM sessions WHERE user_id = ? "
            "AND (clock_out IS NULL OR clock_out > ?)",
            (user["id"], tz.utc_iso(day_start)),
        ).fetchall()
        h = sum(tz.overlap_hours(r["clock_in"], r["clock_out"], day_start, now, now)
                for r in rows)
        _notify(conn, f"🔴 {user['name']} さんが退席しました（本日 {round(h, 1)}h）",
                member_email=user["email"])
    return {"session_id": session["id"], "clock_out": now_iso()}


@app.post("/api/screenshots")
async def upload_screenshot(
    image: UploadFile = File(...),
    tiles: int = Form(1),   # 連結されているモニター枚数(停滞検知をモニター別に行う)
    idle: int | None = Form(None),   # 撮影時点の無操作秒数(稼働率の算出に使用、任意)
    user=Depends(auth_user),
    conn=Depends(get_conn),
):
    if not db.open_session(conn, user["id"]):
        raise HTTPException(409, "Not clocked in")
    # JPEG のみ・サイズ上限を設けてディスク枯渇(DoS)を防ぐ
    if (image.content_type or "").lower() not in ("image/jpeg", "image/jpg"):
        raise HTTPException(415, "JPEG画像のみ受け付けます")
    data = await image.read()
    if len(data) > 6_000_000:
        raise HTTPException(413, "画像が大きすぎます")
    taken_at = datetime.now(timezone.utc)
    rel = f"{user['id']}/{taken_at.strftime('%Y%m%d_%H%M%S')}.jpg"
    dest = SCREENSHOT_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    # --- 画面停滞(無変化)検知 ---
    # 直前のキャプチャと指紋を比べ、一致率がしきい値以上の状態が続いた回数(stall)を
    # 記録する。回数がしきい値に達した瞬間に1回だけ通知する。
    s = db.get_settings(conn)
    # モニター枚数に応じた指紋を作る(モニター別に比較するため)。JPEGデコードはスレッドへ
    sig = await asyncio.to_thread(imaging.signature, data, tiles)
    prev = conn.execute(
        "SELECT taken_at, sig, stall FROM screenshots WHERE user_id = ? "
        "ORDER BY taken_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    sim: float | None = None
    stall = 0
    if sig and prev and prev["sig"]:
        try:
            gap = (taken_at - datetime.fromisoformat(prev["taken_at"])).total_seconds()
        except ValueError:
            gap = None
        # 撮影が長く空いた場合(休憩・再ログイン)は連続扱いにしない
        if gap is not None and gap <= s["capture_max_interval"] * 3:
            sim = imaging.similarity(sig, prev["sig"])
            if sim is not None and sim >= s["stall_threshold"]:
                stall = (prev["stall"] or 0) + 1
    idle_sec = max(0, int(idle)) if idle is not None else None
    cur = conn.execute(
        "INSERT INTO screenshots (user_id, taken_at, path, sig, similarity, stall, idle) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], taken_at.isoformat(), rel, sig,
         round(sim) if sim is not None else None, stall, idle_sec),
    )
    # 直前に操作があった(idle計測あり&閾値未満)なら在席中なので停滞アラートは出さない。
    # 「離席の可能性」の通知が、画面が変わらないだけの作業中に誤発火するのを防ぐ。
    active = idle_sec is not None and idle_sec < s["idle_threshold"]
    if (s["notify_stall"] and user["notify_enabled"]
            and stall == s["stall_alert_count"] and not active):
        n = stall + 1  # ほぼ同一だった連続キャプチャ枚数
        _notify(
            conn,
            f"⚠ {user['name']} さんの画面が直近 {n} 枚連続でほとんど変化していません"
            f"（一致率 {round(sim)}% 以上）。離席の可能性があります。",
            member_email=user["email"],
        )
    return {"screenshot_id": cur.lastrowid}


@app.get("/api/status")
def status(
    team_id: int | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    now = datetime.now(timezone.utc)
    day_start, _ = tz.today_window(now)
    settings = db.get_settings(conn)
    q = """
        SELECT u.id, u.name, u.team_id, t.name AS team_name,
               s.clock_in AS open_since,
               s.category AS open_category,
               (SELECT taken_at FROM screenshots WHERE user_id = u.id
                ORDER BY taken_at DESC LIMIT 1) AS last_screenshot,
               (SELECT stall FROM screenshots WHERE user_id = u.id
                ORDER BY taken_at DESC LIMIT 1) AS last_stall
        FROM users u
        LEFT JOIN teams t ON t.id = u.team_id
        LEFT JOIN sessions s ON s.user_id = u.id AND s.clock_out IS NULL
    """
    args: list = []
    if team_id is not None:
        q += " WHERE u.team_id = ?"
        args.append(team_id)
    q += " ORDER BY u.name"
    rows = conn.execute(q, args).fetchall()
    # 「今日」(ローカルTZ) に重なるセッションだけ取り、Python側で時間を合算
    today_sessions = conn.execute(
        "SELECT user_id, clock_in, clock_out, category FROM sessions "
        "WHERE clock_out IS NULL OR clock_out > ?",
        (tz.utc_iso(day_start),),
    ).fetchall()
    hours: dict[int, float] = {}
    segs: dict[int, list] = {}
    now_local = now.astimezone(tz.TZ)
    for s in today_sessions:
        h = tz.overlap_hours(s["clock_in"], s["clock_out"], day_start, now, now)
        hours[s["user_id"]] = hours.get(s["user_id"], 0.0) + h
        seg_start = max(tz.local(s["clock_in"]), day_start)
        seg_end = min(
            tz.local(s["clock_out"]) if s["clock_out"] else now_local, now_local
        )
        if seg_end > seg_start:
            segs.setdefault(s["user_id"], []).append(
                {
                    "start": seg_start.isoformat(),
                    "end": seg_end.isoformat(),
                    "open": s["clock_out"] is None,
                    "category": s["category"],
                }
            )
    # 本日の稼働率: 無操作秒数が閾値未満のキャプチャ割合 (idleが取れた撮影のみ対象)
    activity: dict[int, int | None] = {}
    for a in conn.execute(
        "SELECT user_id, "
        "SUM(CASE WHEN idle IS NOT NULL THEN 1 ELSE 0 END) AS measured, "
        "SUM(CASE WHEN idle IS NOT NULL AND idle < ? THEN 1 ELSE 0 END) AS active "
        "FROM screenshots WHERE taken_at >= ? GROUP BY user_id",
        (settings["idle_threshold"], tz.utc_iso(day_start)),
    ).fetchall():
        activity[a["user_id"]] = (
            round(a["active"] / a["measured"] * 100) if a["measured"] else None
        )
    return [
        {
            "user_id": r["id"],
            "name": r["name"],
            "team_name": r["team_name"],
            "seated": r["open_since"] is not None,
            "open_since": r["open_since"],
            "category": r["open_category"],
            "hours_today": round(hours.get(r["id"], 0.0), 2),
            "last_screenshot": r["last_screenshot"],
            # 直近キャプチャが連続して無変化(離席の可能性)か。機能OFFなら出さない
            "stalled": bool(settings["notify_stall"]) and r["open_since"] is not None
            and (r["last_stall"] or 0) >= settings["stall_alert_count"],
            "activity": activity.get(r["id"]),   # 本日の稼働率(%) 取得不可は null
            "today_sessions": segs.get(r["id"], []),
        }
        for r in rows
    ]


def _monthly_detail(conn, user, month: str) -> dict:
    """日別の在席時間・セッション・スクショ (タイムライン用) を組み立てる."""
    user_id = user["id"]
    start, end = tz.month_window(month)
    now = datetime.now(timezone.utc)
    days: dict[str, dict] = {}

    def day_of(d):
        key = d.date().isoformat()
        return days.setdefault(
            key, {"date": key, "hours": 0.0, "sessions": [], "screenshots": []}
        )

    by_category: dict[str, float] = {}
    sessions = conn.execute(
        "SELECT id, clock_in, clock_out, category FROM sessions WHERE user_id = ? "
        "AND clock_in < ? AND (clock_out IS NULL OR clock_out > ?) ORDER BY clock_in",
        (user_id, tz.utc_iso(end), tz.utc_iso(start)),
    ).fetchall()
    for s in sessions:
        # 日をまたぐセッションはローカル日付ごとの区間に分割する(共通ヘルパ)
        for seg_start, seg_end, is_open in tz.day_segments(
            s["clock_in"], s["clock_out"], start, end, now
        ):
            day = day_of(seg_start)
            day["sessions"].append(
                {
                    "id": s["id"],
                    "start": seg_start.isoformat(),
                    "end": seg_end.isoformat(),
                    "open": is_open,
                    "category": s["category"],
                    # 修正モーダル用に元セッションの全体時刻も返す
                    "clock_in": tz.local(s["clock_in"]).isoformat(),
                    "clock_out": tz.local(s["clock_out"]).isoformat()
                    if s["clock_out"] else None,
                }
            )
            seg_hours = (seg_end - seg_start).total_seconds() / 3600
            day["hours"] += seg_hours
            cat = s["category"] or "未分類"
            by_category[cat] = by_category.get(cat, 0.0) + seg_hours

    # 画面停滞(離席の可能性)を実績画像で分かるように、一致率・無操作秒・停滞判定も返す
    s_stall = db.get_settings(conn)
    stall_on = bool(s_stall["notify_stall"])
    alert_count = s_stall["stall_alert_count"]
    idle_thr = s_stall["idle_threshold"]
    shots = conn.execute(
        "SELECT id, taken_at, similarity, stall, idle FROM screenshots WHERE user_id = ? "
        "AND taken_at >= ? AND taken_at < ? ORDER BY taken_at",
        (user_id, tz.utc_iso(start), tz.utc_iso(end)),
    ).fetchall()
    for sh in shots:
        t = tz.local(sh["taken_at"])
        day_of(t)["screenshots"].append({
            "id": sh["id"], "taken_at": t.isoformat(),
            "similarity": sh["similarity"], "idle": sh["idle"],
            # 停滞アラート水準に達したキャプチャ(離席の可能性)。設定OFF時は印を付けない
            "stalled": stall_on and (sh["stall"] or 0) >= alert_count,
            "idle_over": sh["idle"] is not None and sh["idle"] >= idle_thr,
        })

    mstart, mend = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    journ = conn.execute(
        "SELECT date FROM journals WHERE user_id = ? AND date >= ? AND date < ? "
        "AND body != ''",   # 空にした日報(通知重複防止で残す行)は未提出扱い
        (user_id, mstart, mend),
    ).fetchall()
    journal_days = {r["date"] for r in journ}
    leaves = {
        r["date"]: {"type": r["leave_type"], "status": r["status"]}
        for r in conn.execute(
            "SELECT date, leave_type, status FROM leave_requests "
            "WHERE user_id = ? AND date >= ? AND date < ?", (user_id, mstart, mend)
        )
    }

    target = db.get_settings(conn)["daily_target_minutes"] / 60
    for key, d in days.items():
        # 予定時間に対する過不足 (在席のあった日のみ)
        d["over"] = round(d["hours"] - target, 2) if d["hours"] > 0 else None
        d["hours"] = round(d["hours"], 2)
    # 在席が無くても日報・休暇のある日を行に含める
    for jd in journal_days | set(leaves):
        day_of(datetime.strptime(jd, "%Y-%m-%d").replace(tzinfo=tz.TZ))
    for key, d in days.items():
        d["has_journal"] = key in journal_days
        d["leave"] = leaves.get(key)
        d.setdefault("over", None)
    return {
        "user": {"id": user["id"], "name": user["name"]},
        "month": month,
        "days": sorted(days.values(), key=lambda d: d["date"]),
        "categories": db.work_categories(conn),
        "by_category": {k: round(v, 2) for k, v in by_category.items()},
        "target_hours": round(target, 2),
    }


def _mask_settings(s: dict, conn) -> dict:
    """秘密情報 (パスワード・APIキー・Notion のシークレット) はレスポンスに含めない(設定済みかだけ返す).

    あわせて、AI の直近の失敗 (読み取り専用・設定画面と受信箱の注意書き用) と、
    次に週ごとのまとめを送る日時 (読み取り専用) を添える。
    """
    s["ai_errors"] = [
        {**e, "message": notify.safe_error(RuntimeError(e["message"]), s)}
        for e in mail.ai_errors()
    ] if str(s.get("anthropic_api_key") or "").strip() else []
    # 今使う種類の一覧 (読み取り専用。メール画面の選択欄用。「その他」は必ず入っている)
    s["mail_category_list"] = mail.category_list(s)
    # 「メール画面のアドレス」が空のときにお知らせに載せるアドレス (読み取り専用。
    # サーバの環境変数から作る。分からなければ空)
    s["mail_page_url_auto"] = mail.env_mail_page_url()
    # 次に週ごとのまとめを送る日時 (UTC ISO。送る設定がオフなら null)
    nxt = mail.next_report_at(s, datetime.now(timezone.utc), mail.report_last_sent(conn))
    s["mail_report_next_at"] = tz.utc_iso(nxt) if nxt else None
    for key in ("smtp_pass", "imap_pass", "anthropic_api_key", "notion_token"):
        s[f"{key}_set"] = bool(s.get(key))
        s[key] = ""
    return s


@app.get("/api/settings")
def get_settings_api(_admin=Depends(require_admin), conn=Depends(get_conn)):
    return _mask_settings(db.get_settings(conn), conn)


# 範囲外のときに画面に出す名前 (メール画面の数値の欄。無いものは設定の名前のまま出す)
SETTING_LABELS = {
    "mail_fetch_interval_min": "自動受信の間隔（分）",
    "mail_fetch_days": "初回に遡る日数",
    "mail_retention_days": "メール保存日数",
    "mail_remind_hours_high": "お知らせまでの時間・優先度[高]（時間）",
    "mail_remind_hours_normal": "お知らせまでの時間・優先度[中]・[低]（時間）",
    "mail_report_weekday": "週ごとのまとめを送る曜日（0=月曜〜6=日曜）",
    "mail_report_hour": "週ごとのまとめを送る時刻（時）",
}
# 変えたら、記録してある受信の失敗を消す設定 (前の設定での失敗は当てにならないので)
IMAP_SETTING_KEYS = ("imap_host", "imap_port", "imap_user", "imap_pass",
                     "imap_folder", "imap_use_ssl", "mail_fetch_enabled")


def _same_setting(key: str, a, b) -> bool:
    """設定の値が同じか (数値の設定は True と 1 のような書き方の違いを気にしない)."""
    if key in db.INT_SETTINGS:
        return int(a) == int(b)
    return str(a) == str(b)


@app.patch("/api/settings")
def patch_settings(
    body: SettingsPatch, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """全社設定の変更。クライアントは次の撮影サイクルから自動反映する."""
    limits = {  # (最小, 最大)
        "capture_min_interval": (30, 86400),
        "capture_max_interval": (30, 86400),
        "capture_quality": (10, 95),
        "capture_blur": (0, 20),
        "capture_enabled": (0, 1),
        "retention_days": (0, 3650),
        "alert_hours": (1, 24),
        "daily_target_minutes": (0, 1440),
        "notify_clock": (0, 1),
        "notify_alert": (0, 1),
        "notify_journal": (0, 1),
        "notify_stall": (0, 1),
        "stall_threshold": (50, 100),
        "stall_alert_count": (1, 100),
        "clockout_reminder": (0, 1),
        "idle_threshold": (10, 86400),
        "imap_use_ssl": (0, 1),
        "mail_fetch_enabled": (0, 1),
        "mail_fetch_interval_min": MAIL_FETCH_INTERVAL_RANGE,
        "mail_auto_draft": (0, 1),
        "mail_retention_days": (0, 3650),
        "mail_fetch_days": (1, 365),
        "notify_mail_high": (0, 1),
        "mail_save_sent": (0, 1),
        "mail_remind_enabled": (0, 1),
        "mail_remind_hours_high": mail.REMIND_HOURS_RANGE,
        "mail_remind_hours_normal": mail.REMIND_HOURS_RANGE,
        "mail_report_enabled": (0, 1),
        "mail_report_weekday": mail.REPORT_WEEKDAY_RANGE,
        "mail_report_hour": mail.REPORT_HOUR_RANGE,
        "notion_ai_fill": (0, 1),
    }
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "変更内容がありません")
    int_changes = {k: int(v) for k, v in changes.items() if k in db.INT_SETTINGS}
    merged = {**db.get_settings(conn), **int_changes}
    for key, val in int_changes.items():
        lo, hi = limits[key]
        if not lo <= val <= hi:
            raise HTTPException(
                400, f"{SETTING_LABELS.get(key, key)} は {lo}〜{hi} の範囲で指定してください")
    if merged["capture_min_interval"] > merged["capture_max_interval"]:
        raise HTTPException(400, "最短間隔は最長間隔以下にしてください")
    # 文字列設定のバリデーション
    if "company_name" in changes and not str(changes["company_name"]).strip():
        raise HTTPException(400, "会社名を入力してください")
    for key in ("work_start", "work_end", "clockout_reminder_time"):
        if key in changes:
            try:
                datetime.strptime(changes[key], "%H:%M")
            except ValueError:
                raise HTTPException(400, f"{key} は HH:MM 形式で入力してください")
    if "timezone" in changes and not tz.set_tz(changes["timezone"]):
        raise HTTPException(400, "不明なタイムゾーンです (例: Asia/Tokyo)")
    for pkey, plabel in (("smtp_port", "SMTP"), ("imap_port", "IMAP")):
        if pkey in changes and str(changes[pkey]).strip():
            port = str(changes[pkey]).strip()
            if not (port.isdigit() and 1 <= int(port) <= 65535):
                raise HTTPException(
                    400, f"{plabel}ポートは1〜65535の数値で指定してください"
                )
            changes[pkey] = port
    if "work_categories" in changes:
        cats = [c.strip() for c in changes["work_categories"].split(",") if c.strip()]
        if not cats:
            raise HTTPException(400, "作業区分を1つ以上入力してください")
        changes["work_categories"] = ",".join(cats)
    for key in ("anthropic_model", "anthropic_classify_model"):
        # 空なら既定のモデルを使う。それ以外はモデル名の形だけ確かめる
        # (新しいモデルも選べるよう一覧とは照合しない。使えない名前は AI 接続テスト等で分かる)
        if key in changes:
            changes[key] = str(changes[key]).strip()
            if changes[key] and not mail.valid_model_name(changes[key]):
                raise HTTPException(
                    400, "モデル名の形式が正しくありません（例: claude-opus-5-5）")
    # 返事をしていないメールのお知らせ: 止めておく時間帯 ("21-8" の形にそろえる) と、
    # お知らせに載せるメール画面のアドレス
    # Notion: シークレット (形だけ確かめる)・データベース (URL を貼っても番号にそろえる)・
    # 列に入れるもの (書き方を確かめる。列が本当にあるかは「Notion接続テスト」で分かる)
    for key, clean in (("mail_remind_quiet", mail.clean_quiet_hours),
                       ("mail_page_url", mail.clean_page_url),
                       ("notion_token", notion.clean_token),
                       ("notion_database_id", notion.clean_database_id),
                       ("notion_property_map", notion.clean_property_map)):
        if key in changes:
            try:
                changes[key] = clean(changes[key])
            except ValueError as e:
                raise HTTPException(400, str(e))

    current = db.get_settings(conn)
    cat_keys = ("mail_categories", "mail_category_keywords", "mail_category_assignees")
    if any(k in changes for k in cat_keys):
        # 種類と担当の3つは互いに関係する (キーワード・担当の行は種類の一覧にある種類だけ)
        # ので、保存済みの値と合わせて確かめる。担当の名前が登録されているかは、
        # 担当の欄を変えたときだけ確かめる (後から無効にした人がいても、ほかの設定は保存できる)
        merged = {k: changes.get(k, current[k]) for k in cat_keys}
        users = None
        if mail.normalize_newlines(merged["mail_category_assignees"]).strip() != \
                mail.normalize_newlines(current["mail_category_assignees"]).strip():
            users = conn.execute(
                "SELECT id, name FROM users WHERE active = 1 ORDER BY id").fetchall()
        try:
            cleaned = mail.clean_category_settings(
                merged["mail_categories"], merged["mail_category_keywords"],
                merged["mail_category_assignees"], users=users)
        except ValueError as e:
            raise HTTPException(400, str(e))
        for key, value in zip(cat_keys, cleaned):
            if key in changes:
                changes[key] = value
    if any(k in changes and str(changes[k]) != str(current.get(k, ""))
           for k in ("anthropic_api_key", "anthropic_model", "anthropic_classify_model")):
        # キーやモデルを変えたら、前の設定での AI の失敗の記録は当てにならないので消す
        mail.clear_ai_errors()
    if any(k in changes and not _same_setting(k, changes[k], current.get(k, ""))
           for k in IMAP_SETTING_KEYS):
        # 受信サーバの設定を変えたら、前の設定での受信の失敗の記録も消す
        # (次の受信で、うまくいかなければまた記録される)
        mail.clear_fetch_error(conn)
    for key, val in changes.items():
        db.set_setting(conn, key, val)
    secret = {"slack_webhook_url", "smtp_pass", "smtp_user",
              "imap_pass", "imap_user", "anthropic_api_key", "notion_token"}
    _audit(conn, admin, "settings_update", detail=", ".join(
        f"{k}=***" if k in secret else f"{k}={v}" for k, v in changes.items()))
    return _mask_settings(db.get_settings(conn), conn)


@app.post("/api/settings/test-notify")
def test_notify(admin=Depends(require_admin), conn=Depends(get_conn)):
    """設定済みの通知チャネルへテスト送信し、成功したチャネルを返す."""
    s = db.get_settings(conn)
    if not notify.channels(s):
        raise HTTPException(400, "通知先 (Slack または メール) が未設定です")
    sent = notify.deliver(s, f"[テスト] zatsumu からの通知です（{admin['name']}）")
    if not sent:
        raise HTTPException(502, "送信に失敗しました。設定値を確認してください")
    return {"sent": sent}


@app.post("/api/settings/test-remind")
def test_remind(admin=Depends(require_admin), conn=Depends(get_conn)):
    """返事をしていないメールのお知らせを、保存済みの送り先へ試しに送る (メール画面の設定用).

    いま「未対応」のメールを、届いてからの時間に関係なく見本として並べる
    (お知らせ済みにはしない)。送れた送り先と、送れなかった送り先を返す。
    """
    s = db.get_settings(conn)
    configured = notify.channels(s)
    if not configured:
        raise HTTPException(
            400, "お知らせの送り先が未設定です。Slack の URL か、通知先メール"
                 "（「返信の送信（SMTP）」の設定も必要）を入れて「保存」してからお試しください")
    subject, text = mail.remind_test_message(conn, s, admin["name"])
    conn.commit()   # 送信 (時間がかかる) の間、DB の書き込みロックを持たない
    sent = notify.deliver(s, text, subject)
    if not sent:
        raise HTTPException(
            502, "送信できませんでした。Slack の URL・通知先メール・SMTP の設定を確かめてください")
    return {"sent": sent, "failed": [c for c in configured if c not in sent]}


class TestEmailBody(BaseModel):
    to: str


@app.post("/api/settings/test-email")
def test_email(
    body: TestEmailBody, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """指定したメールアドレスへテスト送信する (SMTP設定の疎通・宛先確認用)."""
    to = (body.to or "").strip()
    if "@" not in to:
        raise HTTPException(400, "メールアドレスの形式が正しくありません")
    s = db.get_settings(conn)
    if not s.get("smtp_host"):
        raise HTTPException(400, "SMTPが未設定です。SMTPサーバ等を保存してください")
    try:
        notify.send_email(
            {**s, "mail_to": to},   # 宛先だけこのアドレスに差し替えて送る
            "[テスト] zatsumu メール送信テスト",
            f"このメールは {admin['name']} がテスト送信しました。"
            f"届いていればメール通知の設定は正常です。",
        )
    except Exception as e:  # noqa: BLE001  管理者向けに原因を返す(社内利用)
        raise HTTPException(502, f"送信に失敗しました: {notify.safe_error(e, s)}")
    return {"sent": to}


@app.post("/api/settings/test-imap")
def test_imap(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """IMAP設定の疎通確認 (接続・ログイン・フォルダ選択まで試す)."""
    s = db.get_settings(conn)
    if not str(s.get("imap_host", "")).strip():
        raise HTTPException(400, "IMAPが未設定です。受信サーバ等を保存してください")
    try:
        result = mail.test_imap(s)
    except Exception as e:  # noqa: BLE001  管理者向けに原因を返す(社内利用)
        raise HTTPException(502, f"接続に失敗しました: {notify.safe_error(e, s)}")
    return {"ok": True, "count": result["count"],
            "sent_folder": result["sent_folder"],
            "sent_folder_error": notify.safe_error(
                RuntimeError(result["sent_folder_error"]), s)
            if result["sent_folder_error"] else ""}


@app.post("/api/settings/test-ai")
def test_ai(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """保存済みの Claude APIキー・モデルで、AI を実際に呼べるか確かめる.

    返信文用・優先度判定用のモデルに、ごく短い依頼を1回ずつ送る (費用はごくわずか)。
    入力欄に入れただけで保存していないキーは試さない (画面で先に保存を促す)。
    """
    s = db.get_settings(conn)
    if not str(s.get("anthropic_api_key") or "").strip():
        raise HTTPException(
            400, "Claude APIキーが未設定です。キーを入力して保存してからお試しください")
    conn.commit()   # AI の呼び出し (時間がかかる) の間、DB の書き込みロックを持たない
    try:
        result = mail.test_ai(s)
    except Exception as e:  # noqa: BLE001  管理者向けに原因を返す(社内利用)
        raise HTTPException(502, f"AIを使えませんでした: {notify.safe_error(e, s)}")
    return {"ok": True, **result}


@app.post("/api/settings/test-notion")
def test_notion(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """保存済みのシークレットで Notion のデータベースを読めるか確かめる (メール画面の設定用).

    データベースの名前と列の一覧 (種類と、登録するときに何を入れるか) を返す。
    「列に入れるもの」に、このデータベースで使えない行があれば problems に理由を入れる。
    Notion には何も書き込まない。
    """
    s = db.get_settings(conn)
    if not str(s.get("notion_token") or "").strip():
        raise HTTPException(
            400, "Notionのシークレットが未設定です。シークレットを入れて「保存」してからお試しください")
    if not str(s.get("notion_database_id") or "").strip():
        raise HTTPException(
            400, "NotionのデータベースのURLが未設定です。URLを入れて「保存」してからお試しください")
    conn.commit()   # Notion とのやりとり (時間がかかる) の間、DB の書き込みロックを持たない
    try:
        result = notion.test_connection(s)
    except notion.NotionError as e:
        raise HTTPException(502, f"Notionのデータベースを読めませんでした: {notify.safe_error(e, s)}")
    result["problems"] = [notify.safe_error(RuntimeError(p), s) for p in result["problems"]]
    return {"ok": True, **result}


# ---------- 新しい端末のメール確認 (ZATSUMU_LOGIN_VERIFY_EMAIL 設定時のみ) ----------

class DeviceVerifyBody(BaseModel):
    request_id: str
    code: str


def _require_device_feature() -> None:
    if not devices.enabled():
        raise HTTPException(400, "新しい端末の確認は有効になっていません")


@app.post("/api/device/start")
def device_start(
    request: Request, admin=Depends(require_admin_token), conn=Depends(get_conn)
):
    """確認コードを宛先へメールする (管理者トークンのみで呼べる・端末トークン不要)."""
    _require_device_feature()
    ip = devices.client_ip(request)
    ua = request.headers.get("user-agent")
    try:
        out = devices.start_challenge(conn, admin, ip, ua)
    except devices.RateLimited as e:
        raise HTTPException(429, str(e))
    except devices.SendUnavailable:
        _audit(conn, admin, "device_code_send_fail", admin["id"], detail=f"ip={ip}")
        conn.commit()
        # 中身の例外や設定値は出さない (原因はサーバのログに残している)
        raise HTTPException(
            503, "確認コードのメールを送れませんでした。サーバの担当者に連絡してください"
        )
    _audit(conn, admin, "device_code_sent", admin["id"], detail=f"ip={ip}")
    return out


@app.post("/api/device/verify")
def device_verify(
    body: DeviceVerifyBody,
    request: Request,
    admin=Depends(require_admin_token),
    conn=Depends(get_conn),
):
    """確認コードが合えば端末トークンを返す (平文を返すのはこの1回だけ)."""
    _require_device_feature()
    ip = devices.client_ip(request)
    try:
        devices.verify_challenge(conn, admin, body.request_id, body.code)
    except devices.VerifyError as e:
        _audit(conn, admin, "device_verify_fail", admin["id"], detail=f"ip={ip}")
        conn.commit()  # 試行回数を残す (例外で巻き戻らないように)
        raise HTTPException(400, str(e))
    label = request.headers.get("user-agent") or ""
    token = devices.issue_device(conn, admin["id"], label, ip)
    _audit(conn, admin, "device_verify_ok", admin["id"],
           detail=f"ip={ip} {devices.clean_label(label)}")
    return {"device_token": token}


@app.get("/api/devices")
def list_devices_api(
    request: Request, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """確認済みの端末の一覧 (有効なものだけ)。current=いま使っている端末."""
    cur = devices.check_device(conn, admin["id"], request.headers.get("x-device-token"))
    rows = devices.list_devices(conn)
    for r in rows:
        r["current"] = bool(cur) and r["id"] == cur["id"]
    return {"enabled": devices.enabled(), "devices": rows}


@app.delete("/api/devices/{device_id}")
def revoke_device_api(
    device_id: int, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """端末を取り消す (その端末では次の操作から確認コードが必要になる)."""
    row = devices.revoke_device(conn, device_id)
    if not row:
        raise HTTPException(404, "Not found")
    _audit(conn, admin, "device_revoke", row["user_id"],
           detail=f"id={device_id} {row['label']}")
    return {"revoked": device_id}


@app.get("/api/config")
def public_config(conn=Depends(get_conn)):
    """ログイン前でも使う表示用の公開設定 (会社名・勤務時間帯)."""
    s = db.get_settings(conn)
    return {
        "company_name": s["company_name"],
        "work_start": s["work_start"],
        "work_end": s["work_end"],
    }


@app.get("/api/me/settings")
def me_settings(user=Depends(auth_user), conn=Depends(get_conn)):
    """クライアント用の実効設定 (全社設定 + 本人の撮影ON/OFF)."""
    s = db.get_settings(conn)
    return {
        "min_interval": s["capture_min_interval"],
        "max_interval": s["capture_max_interval"],
        "quality": s["capture_quality"],
        "blur": s["capture_blur"],
        "capture_enabled": bool(s["capture_enabled"])
        and bool(user["capture_enabled"]),
    }


@app.get("/api/users")
def list_users(_admin=Depends(require_admin), conn=Depends(get_conn)):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT u.id, u.name, u.token, u.is_admin, u.active, u.capture_enabled, "
            "u.notify_enabled, u.email, u.team_id, t.name AS team_name "
            "FROM users u LEFT JOIN teams t ON t.id = u.team_id ORDER BY u.name"
        )
    ]


@app.get("/api/teams")
def list_teams(_admin=Depends(require_admin), conn=Depends(get_conn)):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT t.id, t.name, "
            "(SELECT COUNT(*) FROM users u WHERE u.team_id = t.id) AS members "
            "FROM teams t ORDER BY t.name"
        )
    ]


@app.post("/api/teams")
def create_team(body: TeamBody, admin=Depends(require_admin), conn=Depends(get_conn)):
    import sqlite3 as _sq

    name = body.name.strip()
    if not name:
        raise HTTPException(400, "チーム名を入力してください")
    try:
        cur = conn.execute("INSERT INTO teams (name) VALUES (?)", (name,))
    except _sq.IntegrityError:
        raise HTTPException(409, "同じ名前のチームが既に存在します")
    _audit(conn, admin, "team_create", detail=name)
    return {"id": cur.lastrowid, "name": name}


@app.patch("/api/teams/{team_id}")
def rename_team(
    team_id: int, body: TeamBody, admin=Depends(require_admin), conn=Depends(get_conn)
):
    if not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
        raise HTTPException(404, "Team not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "チーム名を入力してください")
    conn.execute("UPDATE teams SET name = ? WHERE id = ?", (name, team_id))
    _audit(conn, admin, "team_rename", detail=name)
    return {"id": team_id, "name": name}


@app.delete("/api/teams/{team_id}")
def delete_team(team_id: int, admin=Depends(require_admin), conn=Depends(get_conn)):
    if not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
        raise HTTPException(404, "Team not found")
    # 所属メンバーは未所属に戻す
    conn.execute("UPDATE users SET team_id = NULL WHERE team_id = ?", (team_id,))
    conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    _audit(conn, admin, "team_delete", detail=str(team_id))
    return {"deleted": team_id}


@app.post("/api/users")
def create_user_api(
    body: UserCreate, admin=Depends(require_admin), conn=Depends(get_conn)
):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名前を入力してください")
    import sqlite3 as _sq

    try:
        user = db.create_user(conn, name, is_admin=body.is_admin)
    except _sq.IntegrityError:
        raise HTTPException(409, "同じ名前のメンバーが既に存在します")
    _audit(conn, admin, "user_create", user["id"], detail=name)
    return user  # token はこのレスポンスでのみ返す


@app.patch("/api/users/{user_id}")
def patch_user(
    user_id: int,
    body: UserPatch,
    admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    if user_id == admin["id"] and (body.active is False or body.is_admin is False):
        raise HTTPException(400, "自分自身の権限・状態は変更できません")
    if body.active is not None:
        conn.execute(
            "UPDATE users SET active = ? WHERE id = ?", (int(body.active), user_id)
        )
        _audit(conn, admin, "user_active", user_id, detail=str(body.active))
    if body.is_admin is not None:
        conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (int(body.is_admin), user_id),
        )
        _audit(conn, admin, "user_admin", user_id, detail=str(body.is_admin))
    if body.capture_enabled is not None:
        conn.execute(
            "UPDATE users SET capture_enabled = ? WHERE id = ?",
            (int(body.capture_enabled), user_id),
        )
        _audit(conn, admin, "user_capture", user_id,
               detail=str(body.capture_enabled))
    if body.notify_enabled is not None:
        conn.execute(
            "UPDATE users SET notify_enabled = ? WHERE id = ?",
            (int(body.notify_enabled), user_id),
        )
        _audit(conn, admin, "user_notify", user_id,
               detail=str(body.notify_enabled))
    if body.email is not None:
        email = body.email.strip()
        if email and "@" not in email:
            raise HTTPException(400, "メールアドレスの形式が正しくありません")
        conn.execute(
            "UPDATE users SET email = ? WHERE id = ?", (email, user_id)
        )
        _audit(conn, admin, "user_email", user_id, detail=email or "(削除)")
    if body.clear_team:
        conn.execute("UPDATE users SET team_id = NULL WHERE id = ?", (user_id,))
        _audit(conn, admin, "user_team", user_id, detail="(未所属)")
    elif body.team_id is not None:
        if not conn.execute("SELECT 1 FROM teams WHERE id = ?",
                            (body.team_id,)).fetchone():
            raise HTTPException(400, "不明なチームです")
        conn.execute("UPDATE users SET team_id = ? WHERE id = ?",
                     (body.team_id, user_id))
        _audit(conn, admin, "user_team", user_id, detail=str(body.team_id))
    row = conn.execute(
        "SELECT id, name, is_admin, active, capture_enabled, notify_enabled, "
        "email, team_id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row)


@app.delete("/api/users/{user_id}")
def delete_user(
    user_id: int, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """メンバーを完全削除する (打刻・キャプチャ・日報・申請も全て消す。元に戻せない)."""
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    if user_id == admin["id"]:
        raise HTTPException(400, "自分自身は削除できません")
    # キャプチャ画像ファイルを先に消す(行を消す前にパスを取得)
    for r in conn.execute("SELECT path FROM screenshots WHERE user_id = ?", (user_id,)):
        (SCREENSHOT_DIR / r["path"]).unlink(missing_ok=True)
    # 関連データ → 本体の順に削除 (FK制約に沿う)
    for tbl in ("screenshots", "sessions", "journals", "leave_requests", "corrections",
                "device_tokens", "device_challenges"):
        conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (user_id,))
    # その人が担当のメールは消さずに「担当なし」に戻す
    conn.execute("UPDATE mails SET assignee_user_id = NULL WHERE assignee_user_id = ?",
                 (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    _audit(conn, admin, "user_delete", detail=target["name"])
    return {"deleted": user_id, "name": target["name"]}


@app.post("/api/users/{user_id}/token")
def regenerate_token(
    user_id: int, admin=Depends(require_admin), conn=Depends(get_conn)
):
    import secrets

    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET token = ? WHERE id = ?", (token, user_id))
    # 古いトークンで確認した端末も使えなくする (漏れた疑いで作り直すことが多いため)
    devices.revoke_all(conn, user_id)
    _audit(conn, admin, "token_regen", user_id)
    return {"id": user_id, "name": target["name"], "token": token}


@app.post("/api/users/{user_id}/clock-out")
def force_clock_out(
    user_id: int, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """退席し忘れたメンバーを管理者が強制退席させる."""
    session = db.open_session(conn, user_id)
    if not session:
        raise HTTPException(409, "Not clocked in")
    conn.execute(
        "UPDATE sessions SET clock_out = ? WHERE id = ?", (now_iso(), session["id"])
    )
    _audit(conn, admin, "force_clock_out", user_id, session["id"])
    return {"session_id": session["id"], "clock_out": now_iso()}


@app.post("/api/users/{user_id}/sessions")
def add_session(
    user_id: int,
    body: SessionBody,
    admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """打刻の手動追加 (修正対応)."""
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise HTTPException(404, "User not found")
    ci, co = _parse_ts(body.clock_in), _parse_ts(body.clock_out)
    if co <= ci:
        raise HTTPException(400, "退席は着席より後の時刻にしてください")
    category = _resolve_category(conn, body.category)
    cur = conn.execute(
        "INSERT INTO sessions (user_id, clock_in, clock_out, category) "
        "VALUES (?, ?, ?, ?)",
        (user_id, ci, co, category),
    )
    _audit(conn, admin, "session_add", user_id, cur.lastrowid, f"{ci} - {co}")
    return {"session_id": cur.lastrowid}


@app.patch("/api/sessions/{session_id}")
def edit_session(
    session_id: int,
    body: SessionBody,
    admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """打刻の修正。修正内容は監査ログ(修正履歴)に残る."""
    old = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not old:
        raise HTTPException(404, "Session not found")
    ci, co = _parse_ts(body.clock_in), _parse_ts(body.clock_out)
    if co <= ci:
        raise HTTPException(400, "退席は着席より後の時刻にしてください")
    category = old["category"] if body.category is None \
        else _resolve_category(conn, body.category)
    conn.execute(
        "UPDATE sessions SET clock_in = ?, clock_out = ?, category = ? WHERE id = ?",
        (ci, co, category, session_id),
    )
    _audit(
        conn, admin, "session_edit", old["user_id"], session_id,
        f"{old['clock_in']} - {old['clock_out']} -> {ci} - {co}",
    )
    return {"session_id": session_id}


@app.delete("/api/sessions/{session_id}")
def delete_session(
    session_id: int, admin=Depends(require_admin), conn=Depends(get_conn)
):
    old = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not old:
        raise HTTPException(404, "Session not found")
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    _audit(
        conn, admin, "session_delete", old["user_id"], session_id,
        f"{old['clock_in']} - {old['clock_out']}",
    )
    return {"deleted": session_id}


@app.get("/api/users/{user_id}/monthly")
def user_monthly(
    user_id: int,
    month: str | None = None,
    _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """個人の月次詳細 (管理者用)."""
    month = _validate_month(month)
    user = conn.execute(
        "SELECT id, name FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not user:
        raise HTTPException(404, "User not found")
    return _monthly_detail(conn, user, month)


@app.get("/api/me/monthly")
def me_monthly(
    month: str | None = None, user=Depends(auth_user), conn=Depends(get_conn)
):
    """自分の月次詳細 (メンバー本人用)."""
    month = _validate_month(month)
    return _monthly_detail(conn, user, month)


def _get_journal(conn, user_id: int, date: str):
    row = conn.execute(
        "SELECT body, updated_at FROM journals WHERE user_id = ? AND date = ?",
        (user_id, date),
    ).fetchone()
    return {
        "date": date,
        "body": row["body"] if row else "",
        "updated_at": row["updated_at"] if row else None,
    }


@app.get("/api/me/journal")
def get_my_journal(
    date: str | None = None, user=Depends(auth_user), conn=Depends(get_conn)
):
    """自分の日報(業務報告)を取得."""
    return _get_journal(conn, user["id"], _valid_date(date))


@app.put("/api/me/journal")
def put_my_journal(
    body: JournalBody, user=Depends(auth_user), conn=Depends(get_conn)
):
    """自分の日報を保存 (本文が空なら削除)."""
    date = _valid_date(body.date)
    text = body.body.strip()
    if not text:
        # 行は消さず本文だけ空にする。notified_at を残すことで「空にして書き直し」で
        # 再通知するのを防ぐ。空の日報は body='' 判定で未提出扱い(has_journal/提出数)。
        conn.execute(
            "UPDATE journals SET body = '', updated_at = ? WHERE user_id = ? AND date = ?",
            (now_iso(), user["id"], date),
        )
        return {"date": date, "body": ""}
    # 既に通知済みか(=その日の日報の初回保存でないか)を保存前に確認する。
    # notified_at は upsert で書き換えないので、編集で連投しない。
    prev = conn.execute(
        "SELECT notified_at FROM journals WHERE user_id = ? AND date = ?",
        (user["id"], date),
    ).fetchone()
    already_notified = bool(prev and prev["notified_at"])
    conn.execute(
        "INSERT INTO journals (user_id, date, body, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET body = excluded.body, "
        "updated_at = excluded.updated_at",
        (user["id"], date, text, now_iso()),
    )
    # 日報が保存されたら通知先(mail_to)へ本文を送る。連投防止に1日1回だけ。
    s = db.get_settings(conn)
    if s["notify_journal"] and user["notify_enabled"] and not already_notified:
        _notify(conn, f"📝 {user['name']} さんの日報（{date}）\n\n{text}")
        conn.execute(
            "UPDATE journals SET notified_at = ? WHERE user_id = ? AND date = ?",
            (now_iso(), user["id"], date),
        )
    return {"date": date, "body": text}


@app.get("/api/journals")
def list_journals(
    date: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """指定日の全メンバーの日報一覧 (未提出も分かるよう全員返す)."""
    date = _valid_date(date)
    rows = conn.execute(
        """
        SELECT u.id AS user_id, u.name, j.body, j.updated_at
        FROM users u
        LEFT JOIN journals j ON j.user_id = u.id AND j.date = ?
        WHERE u.active = 1
        ORDER BY u.name
        """,
        (date,),
    ).fetchall()
    return {
        "date": date,
        "entries": [
            {
                "user_id": r["user_id"],
                "name": r["name"],
                "body": r["body"] or "",
                "updated_at": r["updated_at"],
            }
            for r in rows
        ],
    }


@app.get("/api/users/{user_id}/journal")
def get_user_journal(
    user_id: int,
    date: str | None = None,
    _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise HTTPException(404, "User not found")
    return _get_journal(conn, user_id, _valid_date(date))


# ---------------- 休暇・欠勤の申請／承認 ----------------

@app.get("/api/leave/types")
def leave_types(user=Depends(auth_user)):
    return {"types": db.LEAVE_TYPES}


@app.get("/api/me/leave")
def my_leave(user=Depends(auth_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT id, date, leave_type, reason, status FROM leave_requests "
        "WHERE user_id = ? ORDER BY date DESC LIMIT 60",
        (user["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/me/leave")
def request_leave(
    body: LeaveBody, user=Depends(auth_user), conn=Depends(get_conn)
):
    date = _valid_date(body.date)
    if body.leave_type not in db.LEAVE_TYPES:
        raise HTTPException(400, "不明な休暇種別です")
    # 未承認の申請のみ上書き(再申請)できる。承認済みは clobber しない。
    # ON CONFLICT の WHERE で原子的に判定し、SELECT→INSERT 間の競合を防ぐ。
    conn.execute(
        "INSERT INTO leave_requests (user_id, date, leave_type, reason, status, "
        "created_at) VALUES (?, ?, ?, ?, 'pending', ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET leave_type = excluded.leave_type, "
        "reason = excluded.reason, status = 'pending', created_at = excluded.created_at "
        "WHERE leave_requests.status != 'approved'",
        (user["id"], date, body.leave_type, body.reason.strip(), now_iso()),
    )
    final = conn.execute(
        "SELECT status FROM leave_requests WHERE user_id = ? AND date = ?",
        (user["id"], date),
    ).fetchone()
    if final and final["status"] == "approved":
        raise HTTPException(409, "その日は既に承認済みの申請があります")
    s = db.get_settings(conn)
    if s["notify_clock"] or s["notify_alert"]:  # 通知が有効なら申請を周知
        _notify(conn, f"📝 {user['name']} さんが休暇申請（{body.leave_type} / {date}）")
    return {"date": date, "status": "pending"}


@app.delete("/api/me/leave/{leave_id}")
def cancel_leave(leave_id: int, user=Depends(auth_user), conn=Depends(get_conn)):
    row = conn.execute(
        "SELECT * FROM leave_requests WHERE id = ? AND user_id = ?",
        (leave_id, user["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    if row["status"] == "approved":
        raise HTTPException(409, "承認済みの申請は取り消せません（管理者に連絡）")
    conn.execute("DELETE FROM leave_requests WHERE id = ?", (leave_id,))
    return {"deleted": leave_id}


@app.get("/api/leave")
def list_leave(
    status: str | None = None,
    _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """休暇申請の一覧 (status で絞り込み: pending/approved/rejected)."""
    q = (
        "SELECT l.id, l.user_id, u.name, l.date, l.leave_type, l.reason, l.status, "
        "l.created_at FROM leave_requests l JOIN users u ON u.id = l.user_id"
    )
    args: list = []
    if status:
        q += " WHERE l.status = ?"
        args.append(status)
    q += " ORDER BY l.date DESC, u.name"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


@app.post("/api/leave/{leave_id}/decision")
def decide_leave(
    leave_id: int,
    body: DecisionBody,
    admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    row = conn.execute(
        "SELECT l.*, u.name FROM leave_requests l JOIN users u ON u.id = l.user_id "
        "WHERE l.id = ?", (leave_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    if row["status"] != "pending":
        raise HTTPException(409, "既に処理済みの申請です")
    status = "approved" if body.approve else "rejected"
    conn.execute(
        "UPDATE leave_requests SET status = ?, decided_at = ?, decided_by = ? "
        "WHERE id = ?", (status, now_iso(), admin["id"], leave_id),
    )
    _audit(conn, admin, "leave_" + status, row["user_id"],
           detail=f"{row['leave_type']} {row['date']}")
    return {"id": leave_id, "status": status}


# ---------- 勤務時間の修正申請 (スタッフ申請 → 管理者承認で反映) ----------
@app.get("/api/me/corrections")
def my_corrections(user=Depends(auth_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT id, date, requested_in, requested_out, reason, status, created_at "
        "FROM corrections WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 60",
        (user["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/me/corrections")
def request_correction(
    body: CorrectionBody, user=Depends(auth_user), conn=Depends(get_conn)
):
    """勤務時間の修正を申請する (着席/退席の少なくとも一方の希望時刻)."""
    date = _valid_date(body.date)
    rin = _valid_hhmm(body.requested_in)
    rout = _valid_hhmm(body.requested_out)
    if not rin and not rout:
        raise HTTPException(400, "着席・退席の少なくとも一方の時刻を入力してください")
    if rin and rout and rout <= rin:
        raise HTTPException(400, "退席は着席より後の時刻にしてください")
    cur = conn.execute(
        "INSERT INTO corrections (user_id, date, requested_in, requested_out, "
        "reason, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (user["id"], date, rin, rout, body.reason.strip(), now_iso()),
    )
    _notify(conn, f"📝 {user['name']} さんが勤務時間の修正を申請しました（{date}）")
    return {"id": cur.lastrowid, "status": "pending"}


@app.delete("/api/me/corrections/{cid}")
def cancel_correction(cid: int, user=Depends(auth_user), conn=Depends(get_conn)):
    row = conn.execute(
        "SELECT * FROM corrections WHERE id = ? AND user_id = ?", (cid, user["id"])
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    if row["status"] == "approved":
        raise HTTPException(409, "承認済みの申請は取り消せません（管理者に連絡）")
    conn.execute("DELETE FROM corrections WHERE id = ?", (cid,))
    return {"deleted": cid}


@app.get("/api/corrections")
def list_corrections(
    status: str | None = None,
    _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """修正申請の一覧 (status で絞り込み: pending/approved/rejected)."""
    q = (
        "SELECT c.id, c.user_id, u.name, c.date, c.requested_in, c.requested_out, "
        "c.reason, c.status, c.created_at FROM corrections c "
        "JOIN users u ON u.id = c.user_id"
    )
    args: list = []
    if status:
        q += " WHERE c.status = ?"
        args.append(status)
    q += " ORDER BY c.date DESC, u.name"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def _apply_correction(conn, admin, row) -> None:
    """承認された修正をその日のセッションに反映する (失敗時は 400 で中断→ロールバック)."""
    start = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=tz.TZ)
    end = start + timedelta(days=1)

    def to_iso(hhmm: str) -> str:
        h, m = map(int, hhmm.split(":"))
        return start.replace(hour=h, minute=m).astimezone(timezone.utc).isoformat()

    rin = to_iso(row["requested_in"]) if row["requested_in"] else None
    rout = to_iso(row["requested_out"]) if row["requested_out"] else None
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND clock_in < ? "
        "AND (clock_out IS NULL OR clock_out > ?) ORDER BY clock_in",
        (row["user_id"], tz.utc_iso(end), tz.utc_iso(start)),
    ).fetchall()
    if not sessions:
        if not (rin and rout):
            raise HTTPException(
                400, "その日の打刻が無いため、着席と退席の両方がある申請のみ承認できます"
            )
        cur = conn.execute(
            "INSERT INTO sessions (user_id, clock_in, clock_out) VALUES (?, ?, ?)",
            (row["user_id"], rin, rout),
        )
        _audit(conn, admin, "correction_add", row["user_id"], cur.lastrowid,
               f"{rin} - {rout}")
        return
    if len(sessions) == 1:
        s = sessions[0]
        ci = rin or s["clock_in"]
        co = rout if rout else s["clock_out"]
        if co is not None and co <= ci:
            raise HTTPException(400, "退席が着席より後になりません。管理画面で手動修正してください")
        conn.execute(
            "UPDATE sessions SET clock_in = ?, clock_out = ? WHERE id = ?",
            (ci, co, s["id"]),
        )
        _audit(conn, admin, "correction_apply", row["user_id"], s["id"],
               f"{s['clock_in']}/{s['clock_out']} -> {ci}/{co}")
        return
    if rin:
        first = sessions[0]
        if first["clock_out"] and rin >= first["clock_out"]:
            raise HTTPException(400, "着席時刻が最初の区間の退席より後です。管理画面で手動修正してください")
        conn.execute(
            "UPDATE sessions SET clock_in = ? WHERE id = ?", (rin, first["id"])
        )
        _audit(conn, admin, "correction_apply_in", row["user_id"], first["id"],
               f"{first['clock_in']} -> {rin}")
    if rout:
        last = sessions[-1]
        if rout <= last["clock_in"]:
            raise HTTPException(400, "退席時刻が最後の区間の着席より前です。管理画面で手動修正してください")
        conn.execute(
            "UPDATE sessions SET clock_out = ? WHERE id = ?", (rout, last["id"])
        )
        _audit(conn, admin, "correction_apply_out", row["user_id"], last["id"],
               f"{last['clock_out']} -> {rout}")


@app.post("/api/corrections/{cid}/decision")
def decide_correction(
    cid: int,
    body: DecisionBody,
    admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    row = conn.execute(
        "SELECT c.*, u.name FROM corrections c JOIN users u ON u.id = c.user_id "
        "WHERE c.id = ?", (cid,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    if row["status"] != "pending":
        raise HTTPException(409, "既に処理済みの申請です")
    if body.approve:
        _apply_correction(conn, admin, row)   # 反映に失敗したら例外でロールバック
        status = "approved"
    else:
        status = "rejected"
    conn.execute(
        "UPDATE corrections SET status = ?, decided_at = ?, decided_by = ? "
        "WHERE id = ?", (status, now_iso(), admin["id"], cid),
    )
    _audit(conn, admin, "correction_" + status, row["user_id"],
           detail=f"{row['date']} {row['requested_in'] or '—'}〜{row['requested_out'] or '—'}")
    return {"id": cid, "status": status}


@app.get("/api/screenshots")
def list_screenshots(
    user_id: int | None = None,
    limit: int = 20,
    _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    q = "SELECT s.*, u.name FROM screenshots s JOIN users u ON u.id = s.user_id"
    args: list = []
    if user_id is not None:
        q += " WHERE s.user_id = ?"
        args.append(user_id)
    q += " ORDER BY s.taken_at DESC LIMIT ?"
    args.append(limit)
    s = db.get_settings(conn)
    stall_on = bool(s["notify_stall"])
    alert_count = s["stall_alert_count"]
    idle_thr = s["idle_threshold"]
    out = []
    for r in conn.execute(q, args).fetchall():
        d = dict(r)
        d.pop("sig", None)  # 指紋は内部用途のみ。レスポンスには含めない
        # 実績画像で「画面停滞(離席の可能性)」が一目で分かるよう判定を付ける
        d["stalled"] = stall_on and (d.get("stall") or 0) >= alert_count
        d["idle_over"] = d.get("idle") is not None and d["idle"] >= idle_thr
        out.append(d)
    return out


@app.get("/api/screenshots/{screenshot_id}/image")
def screenshot_image(
    screenshot_id: int, user=Depends(auth_user), conn=Depends(get_conn)
):
    """キャプチャ画像の閲覧。管理者と本人のみ (本家 F-Chair+ と同じ権限設計)."""
    row = conn.execute(
        "SELECT user_id, path FROM screenshots WHERE id = ?", (screenshot_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    if not user["is_admin"] and row["user_id"] != user["id"]:
        raise HTTPException(403, "Not allowed")
    return FileResponse(SCREENSHOT_DIR / row["path"], media_type="image/jpeg")


@app.delete("/api/screenshots/{screenshot_id}")
def delete_screenshot(
    screenshot_id: int, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """スクショの削除は管理者のみ (本家 F-Chair+ と同じ権限設計)."""
    row = conn.execute(
        "SELECT path FROM screenshots WHERE id = ?", (screenshot_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    (SCREENSHOT_DIR / row["path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM screenshots WHERE id = ?", (screenshot_id,))
    return {"deleted": screenshot_id}


@app.get("/")
def root():
    # トップページは管理画面へ誘導する
    return RedirectResponse("/admin")


@app.get("/healthz")
def healthz():
    # version: 動いている版 (git のコミット)。公開リポジトリの値なので出しても差し支えない
    return {"status": "ok", "version": os.environ.get("ZATSUMU_VERSION", "")}


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    # 静的ページ。データは全て Bearer 認証付き API 経由で取得する。
    return (Path(__file__).parent / "templates" / "admin.html").read_text(
        encoding="utf-8"
    )


@app.get("/me", response_class=HTMLResponse)
def member_page():
    """メンバー用のWeb打刻ページ (スマホ対応)."""
    return (Path(__file__).parent / "templates" / "member.html").read_text(
        encoding="utf-8"
    )


@app.get("/mail", response_class=HTMLResponse)
def mail_page():
    """メール対応の専用画面 (勤怠管理とは別UI・同じサーバ/DB/管理者トークン)."""
    return (Path(__file__).parent / "templates" / "mail.html").read_text(
        encoding="utf-8"
    )


CLIENT_EXE_NAME = "勤怠管理.exe"
CLIENT_ZIP_NAME = "勤怠管理.zip"


@app.get("/download/client")
def download_client():
    """PC用クライアントを配布する。ログイン画面から取得できるよう認証なし.

    配置先: ZATSUMU_DATA_DIR/downloads/。ウイルス対策ソフトの誤検知に強い
    フォルダ版(勤怠管理.zip)を優先し、無ければ従来の単体exe(勤怠管理.exe)を返す。
    どちらも無ければ404。
    """
    downloads = DATA_DIR / "downloads"
    zip_path = downloads / CLIENT_ZIP_NAME
    if zip_path.exists():
        return FileResponse(
            zip_path, media_type="application/zip", filename=CLIENT_ZIP_NAME
        )
    exe_path = downloads / CLIENT_EXE_NAME
    if exe_path.exists():
        return FileResponse(
            exe_path, media_type="application/octet-stream", filename=CLIENT_EXE_NAME
        )
    raise HTTPException(404, "PCアプリは未配置です（管理者が downloads に配置してください）")


def _validate_month(month: str | None) -> str:
    month = month or datetime.now(tz.TZ).strftime("%Y-%m")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(400, "month must be 'YYYY-MM'")
    return month


def _monthly_activity(conn, month: str) -> dict:
    """{user_id: 当月の稼働率(%)}。idleが取れたキャプチャのみ対象、無ければ None."""
    start, end = tz.month_window(month)
    thr = db.get_settings(conn)["idle_threshold"]
    out: dict[int, int | None] = {}
    for a in conn.execute(
        "SELECT user_id, "
        "SUM(CASE WHEN idle IS NOT NULL THEN 1 ELSE 0 END) AS measured, "
        "SUM(CASE WHEN idle IS NOT NULL AND idle < ? THEN 1 ELSE 0 END) AS active "
        "FROM screenshots WHERE taken_at >= ? AND taken_at < ? GROUP BY user_id",
        (thr, tz.utc_iso(start), tz.utc_iso(end)),
    ).fetchall():
        out[a["user_id"]] = (
            round(a["active"] / a["measured"] * 100) if a["measured"] else None
        )
    return out


@app.get("/api/reports/monthly")
def monthly_report(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    month = _validate_month(month)
    target = db.get_settings(conn)["daily_target_minutes"] / 60
    rows = reports.monthly_report(conn, month, target)
    act = _monthly_activity(conn, month)
    for r in rows:
        r["activity"] = act.get(r["user_id"])   # 当月の稼働率(%) 取得不可は null
    return {"month": month, "target_hours": round(target, 2), "rows": rows}


@app.get("/api/reports/summary")
def reports_summary(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """集計グラフ用: 日別推移・作業区分別・チーム別の総労働時間 (全社)."""
    month = _validate_month(month)
    return {"month": month, **reports.summary(conn, month)}


@app.get("/api/reports/monthly.csv", response_class=PlainTextResponse)
def monthly_report_csv(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    month = _validate_month(month)
    target = db.get_settings(conn)["daily_target_minutes"] / 60
    csv_text = reports.report_to_csv(reports.monthly_report(conn, month, target), month)
    return Response(
        content="﻿" + csv_text,  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="zatsumu_{month}.csv"'
        },
    )


@app.get("/api/reports/payroll.csv", response_class=PlainTextResponse)
def payroll_csv_api(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """給与ソフト取込用: 社員ごとの当月実績 (時:分 併記)."""
    month = _validate_month(month)
    target = db.get_settings(conn)["daily_target_minutes"] / 60
    return Response(
        content="﻿" + reports.payroll_csv(conn, month, target),  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="zatsumu_payroll_{month}.csv"'
        },
    )


@app.get("/api/reports/sessions.csv", response_class=PlainTextResponse)
def sessions_csv(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """在席データ: 当月の全打刻 (着席/退席) の生データ CSV."""
    month = _validate_month(month)
    return Response(
        content="﻿" + reports.sessions_csv(conn, month),  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="zatsumu_sessions_{month}.csv"'
        },
    )


@app.get("/api/reports/daily.csv", response_class=PlainTextResponse)
def daily_csv_api(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """日別集計: 日付×メンバーの在席時間マトリクス."""
    month = _validate_month(month)
    return Response(
        content="﻿" + reports.daily_csv(conn, month),  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="zatsumu_daily_{month}.csv"'
        },
    )


@app.get("/api/reports/daily-by-category.csv", response_class=PlainTextResponse)
def daily_by_category_csv_api(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """日別×区分: 日付・メンバー・作業区分ごとの在席時間 (縦持ち・ピボット向き)."""
    month = _validate_month(month)
    return Response(
        content="﻿" + reports.daily_by_category_csv(conn, month),  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="zatsumu_daily_category_{month}.csv"'
        },
    )


@app.get("/api/reports/category.csv", response_class=PlainTextResponse)
def category_csv_api(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """区分別集計(月次): メンバー×作業区分の合計時間マトリクス."""
    month = _validate_month(month)
    return Response(
        content="﻿" + reports.category_totals_csv(conn, month),  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="zatsumu_category_{month}.csv"'
        },
    )


@app.get("/api/reports/audit.csv", response_class=PlainTextResponse)
def audit_csv(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """修正履歴: 管理者操作 (打刻修正・強制退席・ユーザー管理) の監査ログ CSV."""
    import csv
    import io

    month = _validate_month(month)
    start, end = tz.month_window(month)
    rows = conn.execute(
        """
        SELECT a.at, adm.name AS admin_name, a.action, tgt.name AS target_name,
               a.session_id, a.detail
        FROM audit_log a
        JOIN users adm ON adm.id = a.admin_id
        LEFT JOIN users tgt ON tgt.id = a.target_user_id
        WHERE a.at >= ? AND a.at < ? ORDER BY a.at
        """,
        (tz.utc_iso(start), tz.utc_iso(end)),
    ).fetchall()
    labels = {
        "user_create": "メンバー追加",
        "user_active": "有効/無効切替",
        "user_admin": "管理者権限変更",
        "user_capture": "撮影ON/OFF",
        "user_notify": "通知ON/OFF",
        "user_email": "通知メール変更",
        "user_team": "チーム割当変更",
        "user_delete": "メンバー削除",
        "token_regen": "トークン再発行",
        "force_clock_out": "強制退席",
        "session_add": "打刻追加",
        "session_edit": "打刻修正",
        "session_delete": "打刻削除",
        "settings_update": "設定変更",
        "team_create": "チーム作成",
        "team_rename": "チーム改名",
        "team_delete": "チーム削除",
        "leave_approved": "休暇申請を承認",
        "leave_rejected": "休暇申請を却下",
        "correction_add": "修正申請で打刻追加",
        "correction_apply": "修正申請を反映",
        "correction_apply_in": "修正申請(着席)を反映",
        "correction_apply_out": "修正申請(退席)を反映",
        "correction_approved": "修正申請を承認",
        "correction_rejected": "修正申請を却下",
        "mail_send": "メール返信送信",
        "mail_send_refused": "メール返信送信（宛先に届かず）",
        "mail_update": "メール状態変更",
        "mail_report_send": "メールの週ごとのまとめを送信",
        "mail_notion": "メールをNotionに登録",
        "device_code_sent": "端末確認コード送信",
        "device_code_send_fail": "端末確認コード送信失敗",
        "device_verify_ok": "端末確認成功",
        "device_verify_fail": "端末確認失敗",
        "device_revoke": "端末取り消し",
        "device_issue": "端末トークン発行(サーバ)",
        "device_revoke_all": "端末を全て取り消し(サーバ)",
    }
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["日時", "操作者", "操作", "対象メンバー", "詳細"])
    for r in rows:
        writer.writerow(
            [
                tz.local(r["at"]).strftime("%Y-%m-%d %H:%M:%S"),
                r["admin_name"],
                labels.get(r["action"], r["action"]),
                r["target_name"] or "",
                r["detail"] or "",
            ]
        )
    return Response(
        content="﻿" + buf.getvalue(),  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="zatsumu_audit_{month}.csv"'
        },
    )


@app.post("/api/admin/purge")
def purge_now(_admin=Depends(require_admin), conn=Depends(get_conn)):
    days = db.get_settings(conn)["retention_days"]
    deleted = retention.purge_old_screenshots(conn, SCREENSHOT_DIR, days)
    return {"deleted": deleted, "retention_days": days}


# ===================== メール (受信箱・優先度・AI返信) =====================

# 一覧・詳細で使う SELECT (担当の人の名前と、添付ファイルの数も一緒に取る。
# 添付は保存しなかったものも数える。一覧で 📎 を出す用)。
# 一覧用 (_MAIL_LIST_SELECT) は「やり取り N件」を1行ずつ数えない。同じ相手から何千通も
# 届いていると、絞り込みに合う全部の行で数えることになり、一覧がとても遅くなるため
# (並べ替えより先に数える)。一覧は出す行の相手ごとにまとめて数える (_thread_counts)
_MAIL_LIST_SELECT = (
    "SELECT m.*, u.name AS assignee_name, lower(m.from_addr) AS from_key,"
    " (SELECT COUNT(*) FROM mail_attachments a WHERE a.mail_id = m.id) AS attachment_count"
    " FROM mails m LEFT JOIN users u ON u.id = m.assignee_user_id"
)
# 1通だけ取るとき用。thread_count は同じ相手 (差出人のアドレス。大文字・小文字は区別しない)
# から届いたほかのメールの数 (「やり取り N件」用。索引 idx_mails_from_lower で数える)
_MAIL_SELECT = (
    "SELECT m.*, u.name AS assignee_name,"
    " (SELECT COUNT(*) FROM mail_attachments a WHERE a.mail_id = m.id) AS attachment_count,"
    " CASE WHEN m.from_addr = '' THEN 0 ELSE (SELECT COUNT(*) FROM mails t"
    " WHERE lower(t.from_addr) = lower(m.from_addr) AND t.id != m.id) END AS thread_count"
    " FROM mails m LEFT JOIN users u ON u.id = m.assignee_user_id"
)


def _thread_counts(conn, rows) -> dict:
    """一覧に出す行の相手ごとに、届いたメールの数を1回でまとめて数える.

    {lower(差出人のアドレス): 通数} を返す (このメール自身も入る。使う側で1を引く)。
    相手ごとに索引 idx_mails_from_lower の範囲を1回読むだけなので、同じ相手から
    たくさん届いていても、数える量はメールの数を超えない。
    """
    keys = sorted({r["from_key"] for r in rows if r["from_key"]})
    counts = {}
    for i in range(0, len(keys), 400):   # SQLite の ? の数の上限より少なく区切る
        part = keys[i:i + 400]
        counts.update(conn.execute(
            "SELECT lower(from_addr), COUNT(*) FROM mails WHERE lower(from_addr) IN ("
            + ",".join("?" * len(part)) + ") GROUP BY lower(from_addr)", part).fetchall())
    return counts


def _mail_summary(r, thread_count: int | None = None) -> dict:
    """一覧に出す項目 (r は _MAIL_SELECT で取った行。一覧では _MAIL_LIST_SELECT で取った行と、
    _thread_counts で数えた thread_count)."""
    return {
        "id": r["id"],
        "from_addr": r["from_addr"],
        "from_name": r["from_name"],
        "subject": r["subject"],
        "received_at": r["received_at"],
        "priority": r["priority"],
        "priority_reason": r["priority_reason"],
        "priority_source": r["priority_source"],
        "status": r["status"],
        "has_draft": bool(r["draft_reply"]),
        "replied_at": r["replied_at"],
        "attachment_count": r["attachment_count"],
        # 同じ相手から届いたほかのメールの数 (0 なら一覧に「やり取り」を出さない)
        "thread_count": r["thread_count"] if thread_count is None else thread_count,
        # 種類 (空=未分類。この機能より前に取り込んだメール) と担当
        "category": r["category"],
        "assignee_user_id": r["assignee_user_id"],
        "assignee_name": r["assignee_name"] or "",
    }


def _mail_or_404(conn, mail_id: int):
    row = conn.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return row


@app.get("/api/mail")
def list_mail(
    status: str | None = None,
    priority: int | None = None,
    category: str | None = None,
    assignee: str | None = None,
    admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """受信メール一覧 (優先度[高]が上・同一優先度は新しい順).

    category は種類の名前 (none で未分類)、assignee は担当の人の番号
    (none で担当なし、me で自分の担当) で絞り込む。
    """
    where, params = [], []
    if status:
        if status not in mail.MAIL_STATUSES:
            raise HTTPException(400, f"不明な状態です: {status}")
        where.append("m.status = ?")
        params.append(status)
    if priority is not None:
        if priority not in (1, 2, 3):
            raise HTTPException(400, "priority は 1〜3 で指定してください")
        where.append("m.priority = ?")
        params.append(priority)
    if category:
        # 一覧から消した種類のメールも探せるよう、設定の一覧とは照らし合わせない
        where.append("m.category = ?")
        params.append("" if category == "none" else category)
    if assignee:
        if assignee == "none":
            where.append("m.assignee_user_id IS NULL")
        else:
            if assignee == "me":
                uid = admin["id"]
            elif assignee.isascii() and assignee.isdigit():
                uid = int(assignee)
            else:
                raise HTTPException(400, "assignee は担当の人の番号か none / me で指定してください")
            where.append("m.assignee_user_id = ?")
            params.append(uid)
    sql = _MAIL_LIST_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.priority, m.received_at DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    counts = _thread_counts(conn, rows)
    return [_mail_summary(r, max(0, counts.get(r["from_key"], 1) - 1) if r["from_key"] else 0)
            for r in rows]


@app.post("/api/mail/fetch")
def fetch_mail_now(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """今すぐ IMAP から新着を取り込む (メール画面の「今すぐ受信」用).

    ほかの受信 (定期受信・別の画面の「今すぐ受信」) の途中なら、終わるのを少し待ってから
    受信する。それでも終わらなければ 409 (少し待ってから押してもらう)。
    """
    s = db.get_settings(conn)
    if not str(s.get("imap_host", "")).strip():
        raise HTTPException(400, "IMAPが未設定です。設定画面で受信サーバを保存してください")
    conn.commit()   # 受信 (時間がかかる) の間、DB の書き込みロックを持たない
    try:
        added = mail.fetch_mail_locked(conn, s, attachment_dir=MAIL_ATTACHMENT_DIR,
                                       wait=FETCH_WAIT_SEC)
    except mail.FetchBusy as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001  管理者向けに原因を返す(社内利用)
        reason = notify.safe_error(RuntimeError(mail.fetch_error_text(e)), s)
        raise HTTPException(502, f"受信に失敗しました: {reason}")
    return {"fetched": len(added)}


# ----- 受信の様子 (メール画面が1分ごとに聞く) -----
# /api/mail/{mail_id} より先に宣言する (後にすると "status" が mail_id として読まれ、422 になる)

@app.get("/api/mail/status")
def mail_status(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """受信の様子 (開いたままのメール画面が新着に気付くための、軽い問い合わせ).

    未対応の数と一番新しいメールの番号 (どちらかが変わったら画面が受信箱を取り直す)、
    最後に受信できた時刻、直近の受信が失敗していればその理由 (分かりやすい言葉) を返す。
    1分ごと・タブごとに呼ばれるので、DB は読むだけにする (書き込みのロックを取らない)。
    """
    s = db.get_settings(conn)
    unhandled, newest = conn.execute(
        "SELECT (SELECT COUNT(*) FROM mails WHERE status = 'unhandled'),"
        " (SELECT MAX(id) FROM mails)").fetchone()
    imap_set = bool(str(s.get("imap_host") or "").strip())
    return {
        "unhandled": unhandled,
        "newest_id": newest,
        **mail.fetch_state(conn),
        "imap_set": imap_set,
        "auto_fetch": imap_set and bool(s["mail_fetch_enabled"]),
        "interval_min": s["mail_fetch_interval_min"],
    }


# ----- 定型文 (よく使う返信文) -----
# /api/mail/templates... は /api/mail/{mail_id} より先に宣言する
# (後にすると "templates" が mail_id として読まれ、422 になる)

def _template_or_404(conn, tid: int):
    row = conn.execute("SELECT * FROM mail_templates WHERE id = ?", (tid,)).fetchone()
    if not row:
        raise HTTPException(404, "定型文が見つかりません")
    return row


def _template_dict(r) -> dict:
    return {"id": r["id"], "name": r["name"], "body": r["body"],
            "sort_order": r["sort_order"], "updated_at": r["updated_at"]}


def _clean_template(conn, name: str | None, body: str | None,
                    tid: int | None = None) -> dict:
    """定型文の名前・本文を整えて確かめる (渡された項目だけ)."""
    out = {}
    if name is not None:
        name = " ".join(name.split())   # 名前は1行 (改行・連続した空白は空白1つに)
        if not name:
            raise HTTPException(400, "定型文の名前を入力してください")
        if len(name) > mail.TEMPLATE_NAME_MAX_CHARS:
            raise HTTPException(
                400, f"定型文の名前は{mail.TEMPLATE_NAME_MAX_CHARS}文字以内にしてください")
        if conn.execute("SELECT 1 FROM mail_templates WHERE name = ? AND id != ?",
                        (name, tid or 0)).fetchone():
            raise HTTPException(409, "同じ名前の定型文が既にあります")
        out["name"] = name
    if body is not None:
        body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            raise HTTPException(400, "定型文の本文を入力してください")
        if len(body) > mail.TEMPLATE_BODY_MAX_CHARS:
            raise HTTPException(
                400, f"定型文の本文は{mail.TEMPLATE_BODY_MAX_CHARS}文字以内にしてください")
        out["body"] = body
    return out


def _templates_in_order(conn):
    return conn.execute("SELECT * FROM mail_templates ORDER BY sort_order, id").fetchall()


@app.get("/api/mail/templates")
def list_mail_templates(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """定型文の一覧 (並び順。差し込みは置き換えない)."""
    return [_template_dict(r) for r in _templates_in_order(conn)]


@app.post("/api/mail/templates")
def create_mail_template(
    body: MailTemplateBody, admin=Depends(require_admin), conn=Depends(get_conn)
):
    """定型文を追加する (一覧の一番下に入る)."""
    fields = _clean_template(conn, body.name, body.body)
    count = conn.execute("SELECT COUNT(*) FROM mail_templates").fetchone()[0]
    if count >= mail.TEMPLATE_MAX_COUNT:
        raise HTTPException(400, f"定型文は{mail.TEMPLATE_MAX_COUNT}件までです。"
                                 "使わないものを消してから追加してください")
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO mail_templates (name, body, sort_order, created_at, updated_at)"
        " VALUES (?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM mail_templates),"
        " ?, ?)",
        (fields["name"], fields["body"], now, now),
    )
    _audit(conn, admin, "mail_template_create", detail=fields["name"])
    return _template_dict(_template_or_404(conn, cur.lastrowid))


@app.patch("/api/mail/templates/{tid}")
def patch_mail_template(
    tid: int, body: MailTemplatePatch, admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """定型文の名前・本文の変更と、並び順の入れ替え (move: up / down)."""
    row = _template_or_404(conn, tid)
    if body.name is None and body.body is None and body.move is None:
        raise HTTPException(400, "変更内容がありません")
    if body.move is not None and body.move not in ("up", "down"):
        raise HTTPException(400, "move は up か down で指定してください")
    fields = _clean_template(conn, body.name, body.body, tid)
    if fields:
        conn.execute(
            "UPDATE mail_templates SET name = ?, body = ?, updated_at = ? WHERE id = ?",
            (fields.get("name", row["name"]), fields.get("body", row["body"]),
             now_iso(), tid),
        )
        _audit(conn, admin, "mail_template_update",
               detail=f"template={tid} {fields.get('name', row['name'])}")
    if body.move:
        # 隣と入れ替えて、全体を 0,1,2… と振り直す (同じ番号が混ざっていても崩れない)。
        # 一番上の「上へ」・一番下の「下へ」は何もしない
        ids = [r["id"] for r in _templates_in_order(conn)]
        i = ids.index(tid)
        j = i - 1 if body.move == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        conn.executemany("UPDATE mail_templates SET sort_order = ? WHERE id = ?",
                         [(n, x) for n, x in enumerate(ids)])
    return _template_dict(_template_or_404(conn, tid))


@app.delete("/api/mail/templates/{tid}")
def delete_mail_template(
    tid: int, admin=Depends(require_admin), conn=Depends(get_conn)
):
    row = _template_or_404(conn, tid)
    conn.execute("DELETE FROM mail_templates WHERE id = ?", (tid,))
    _audit(conn, admin, "mail_template_delete", detail=f"template={tid} {row['name']}")
    return {"deleted": tid}


# ----- 担当に選べる人 -----
# /api/mail/{mail_id} より先に宣言する (定型文と同じ理由)

@app.get("/api/mail/assignees")
def list_mail_assignees(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """担当に選べる人 (有効な人) の番号と名前 (メール画面の選択欄用).

    メール専用サーバでは Caddy の許可リストが /api/users を通さないので、
    メール画面にはここで返す (トークンなどは返さない)。
    """
    return [{"id": r["id"], "name": r["name"]} for r in conn.execute(
        "SELECT id, name FROM users WHERE active = 1 ORDER BY name, id")]


# ----- 週ごとのまとめ -----
# /api/mail/report/... は /api/mail/{mail_id} より先に宣言する (定型文と同じ理由)

@app.get("/api/mail/report/preview")
def mail_report_preview(_admin=Depends(require_admin), conn=Depends(get_conn)):
    """週ごとのまとめの見本 (昨日までの7日間の分。メール画面の設定で見る用。読むだけ).

    送られる文 (件名・本文) と、まとめた期間 (集計のタイムゾーンの日付。end は最後の日)、
    次に送る日時、前に送った週の「送る日」、いまの送り先を返す。
    """
    s = db.get_settings(conn)
    now = datetime.now(timezone.utc)
    day = now.astimezone(tz.TZ).date()
    subject, text = mail.weekly_report(conn, s, now, day)
    last_sent = mail.report_last_sent(conn)
    nxt = mail.next_report_at(s, now, last_sent)
    return {
        "subject": subject,
        "text": text,
        "start": (day - timedelta(days=mail.REPORT_DAYS)).isoformat(),
        "end": (day - timedelta(days=1)).isoformat(),
        "next_at": tz.utc_iso(nxt) if nxt else None,
        "last_sent_for": last_sent or None,
        "channels": notify.channels(s),
    }


@app.post("/api/mail/report/send-now")
def mail_report_send_now(admin=Depends(require_admin), conn=Depends(get_conn)):
    """週ごとのまとめ (昨日までの7日間の分) を、いまの送り先へすぐ送る (試す用).

    送る設定がオフでも送れる。決まった曜日・時刻に送る分とは別に数える
    (送ったことにはしないので、その週の分もいつもどおり送る)。
    送れた送り先と、送れなかった送り先を返す。
    """
    s = db.get_settings(conn)
    configured = notify.channels(s)
    if not configured:
        raise HTTPException(
            400, "送り先が未設定です。Slack の URL か、通知先メール"
                 "（「返信の送信（SMTP）」の設定も必要）を入れて「保存」してからお試しください")
    subject, text = mail.weekly_report(conn, s, datetime.now(timezone.utc),
                                       sent_by=admin["name"])
    conn.commit()   # 送信 (時間がかかる) の間、DB の書き込みロックを持たない
    sent = notify.deliver(s, text, subject)
    if not sent:
        raise HTTPException(
            502, "送信できませんでした。Slack の URL・通知先メール・SMTP の設定を確かめてください")
    _audit(conn, admin, "mail_report_send", detail="sent=" + ",".join(sent))
    return {"sent": sent, "failed": [c for c in configured if c not in sent]}


@app.get("/api/mail/{mail_id}/templates")
def mail_templates_for_mail(
    mail_id: int, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """このメール用に差し込み ({相手の名前} など) を置き換えた定型文 (返信画面用).

    body は返信文の途中に入れる用、body_with_signature は返信文が空のときに
    丸ごと入れる用 (「返信文を生成」と同じく末尾に署名が付く)。
    """
    r = dict(_mail_or_404(conn, mail_id))
    s = db.get_settings(conn)
    now = datetime.now(timezone.utc)
    out = []
    for t in _templates_in_order(conn):
        text = mail.render_template(t["body"], r, now)
        out.append({"id": t["id"], "name": t["name"], "body": text,
                    "body_with_signature": mail.append_signature(s, text)})
    return out


@app.get("/api/mail/{mail_id}")
def mail_detail(
    mail_id: int, thread: bool = True, _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """メールの詳細 (本文・返信・添付・この相手とのやり取り).

    thread=false なら「この相手とのやり取り」を探さない (返信画面が、やり取りの1通の
    全文を読むとき用)。
    """
    r = conn.execute(_MAIL_SELECT + " WHERE m.id = ?", (mail_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    atts = conn.execute("SELECT * FROM mail_attachments WHERE mail_id = ? ORDER BY id",
                        (mail_id,)).fetchall()
    # 同じ相手とのやり取り (新しい順。こちらが送った返信も付く)
    history = (mail.mail_thread(conn, r) if thread
               else {"thread": [], "thread_total": 0})
    return {
        **_mail_summary(r),
        "to_addr": r["to_addr"],
        "cc_addr": r["cc_addr"],
        "body": r["body"],
        "draft_reply": r["draft_reply"],
        "draft_source": r["draft_source"],
        "draft_generated_at": r["draft_generated_at"],
        # 「全員に返信」で CC に入れる候補 (元の To・Cc の人。自分たちと差出人は除く)
        "reply_cc_candidates": mail.reply_all_cc(db.get_settings(conn), dict(r)),
        "reply_subject": r["reply_subject"],
        "reply_body": r["reply_body"],
        "reply_cc": mail.split_addresses(r["reply_cc"]),
        "reply_bcc": mail.split_addresses(r["reply_bcc"]),
        "replied_by": r["replied_by"],
        # 添付ファイル (保存したもの) と、大きすぎるなどで保存しなかったもの (名前と理由)
        "attachments": [mail.attachment_info(a) for a in atts if not a["skip_reason"]],
        "skipped_attachments": [mail.skipped_attachment_info(a)
                                for a in atts if a["skip_reason"]],
        # Notion に登録したページ (空=まだ) と、登録した時刻・人
        "notion_page_url": r["notion_page_url"],
        "notion_registered_at": r["notion_registered_at"],
        "notion_registered_by": r["notion_registered_by"],
        **history,
    }


@app.get("/api/mail/{mail_id}/attachments/{att_id}")
def mail_attachment(
    mail_id: int, att_id: int, inline: bool = False,
    _admin=Depends(require_admin), conn=Depends(get_conn),
):
    """添付ファイルの中身 (管理者のみ。そのメールの添付でなければ 404).

    画面は fetch で受け取って保存・表示する (トークンを付けるため)。
    中身を見て確かめた画像 (png/jpeg/gif/webp) だけ、その種類で返し、
    ?inline=1 で表示用にできる。それ以外 (SVG・HTML を含む) は必ず
    「ただのデータ」(application/octet-stream) のダウンロードにする。
    ブラウザが中身から種類を推測して開かないよう nosniff を付ける。
    """
    row = conn.execute(
        "SELECT * FROM mail_attachments WHERE id = ? AND mail_id = ?",
        (att_id, mail_id),
    ).fetchone()
    if not row or row["skip_reason"]:
        raise HTTPException(404, "添付ファイルが見つかりません")
    path = mail.attachment_file(MAIL_ATTACHMENT_DIR, row["path"])
    if path is None or not path.is_file():
        raise HTTPException(404, "添付ファイルが見つかりません（削除された可能性があります）")
    preview = row["content_type"] in mail.PREVIEW_TYPES
    kind = "inline" if inline and preview else "attachment"
    return FileResponse(
        path,
        media_type=row["content_type"] if preview else mail.OCTET_STREAM,
        headers={
            "Content-Disposition": mail.content_disposition(kind, row["filename"]),
            "X-Content-Type-Options": "nosniff",
            # お客様のファイルなので、ブラウザや途中のサーバに残さない
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/api/mail/{mail_id}/draft")
def mail_generate_draft(
    mail_id: int,
    body: MailDraftBody,
    _admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """返信の下書きを(再)生成する。APIキー未設定時は定型文になる.

    APIキーがあるのに AI で作れなかった場合は、定型文と一緒に理由 (ai_error) を返す。
    """
    r = _mail_or_404(conn, mail_id)
    s = db.get_settings(conn)
    conn.commit()   # AI の呼び出し (時間がかかる) の間、DB の書き込みロックを持たない
    text, source = mail.generate_reply(s, dict(r), body.instructions)
    ai_error = ""
    if source != "ai" and str(s.get("anthropic_api_key") or "").strip():
        ai_error = notify.safe_error(RuntimeError(
            mail.last_ai_error("draft") or "AIで返信文を作れませんでした"), s)
    conn.execute(
        "UPDATE mails SET draft_reply = ?, draft_source = ?,"
        " draft_generated_at = ? WHERE id = ?",
        (text, source, now_iso(), mail_id),
    )
    return {"draft_reply": text, "draft_source": source, "ai_error": ai_error}


@app.post("/api/mail/{mail_id}/events")
def mail_find_events(
    mail_id: int, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    """このメールに書かれた日付から、カレンダーに入れる予定の候補を作る (返信画面用).

    APIキーがあれば AI に読み取らせ、無い・失敗したときは文中の日付をルールで探す。
    DB には何も書かない。Google などへもサーバからは何も送らない (登録は画面が、
    担当者の確かめた内容で Google カレンダーの登録画面を開くか、予定ファイルを作る)。
    APIキーがあるのに AI で読み取れなかったときは、理由 (ai_error) も返す。
    """
    r = dict(_mail_or_404(conn, mail_id))
    s = db.get_settings(conn)
    conn.commit()   # AI の呼び出し (時間がかかる) の間、DB の書き込みロックを持たない
    out = mail_events.find_events(s, r)
    if out["ai_error"]:
        out["ai_error"] = notify.safe_error(RuntimeError(out["ai_error"]), s)
    return out


@app.post("/api/mail/{mail_id}/notion")
def mail_to_notion(
    mail_id: int, body: MailNotionBody | None = None, admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """このメールを Notion のデータベース (案件DB) に1行で登録する (返信画面の「Notionに登録」).

    列には「列に入れるもの」(設定) と、AI を使うときは AI がメールから読み取った値を入れ、
    ページの中には差出人・受信日時・本文を、添付ファイルは Notion に送って並べる。
    登録済みのメールは、again (もう一度登録) が無ければ 409 (二重に登録しない)。
    同じメールを今ほかの画面が登録しているときも 409。
    ページを作れたら、添付ファイルを載せる前に「登録した」ことを記録して確定する
    (添付で失敗・時間切れになっても、押し直して二重に登録しないように)。
    """
    again = bool(body and body.again)
    _mail_or_404(conn, mail_id)
    s = db.get_settings(conn)
    if not notion.configured(s):
        raise HTTPException(
            400, "Notionの設定がまだです。「設定」の「Notion（案件DB）」でシークレットと"
                 "データベースのURLを保存してください")
    try:
        with notion.registering(mail_id):
            # 待っている間に、ほかの画面が登録を終えていることもあるので、ここで読み直す
            r = conn.execute(_MAIL_SELECT + " WHERE m.id = ?", (mail_id,)).fetchone()
            if not r:
                raise HTTPException(404, "Not found")
            if r["notion_page_url"] and not again:
                when = (tz.local(r["notion_registered_at"]).strftime("%Y-%m-%d %H:%M")
                        if r["notion_registered_at"] else "")
                who = "・".join(x for x in (when, r["notion_registered_by"]) if x)
                raise HTTPException(
                    409, "このメールはもうNotionに登録しています" + (f"（{who}）" if who else "")
                         + "。「Notionで開く」から開けます")
            atts = [dict(a) for a in conn.execute(
                "SELECT * FROM mail_attachments WHERE mail_id = ? ORDER BY id", (mail_id,))]
            conn.commit()   # Notion とのやりとり (時間がかかる) の間、DB の書き込みロックを持たない
            recorded = {}

            def record(page_id: str, url: str) -> None:
                # ページを作れたら、添付ファイルを載せる前に記録して確定する
                at = now_iso()
                try:
                    conn.execute(
                        "UPDATE mails SET notion_page_id = ?, notion_page_url = ?,"
                        " notion_registered_at = ?, notion_registered_by = ? WHERE id = ?",
                        (page_id, url, at, admin["name"], mail_id))
                    _audit(conn, admin, "mail_notion",
                           detail=f"mail={mail_id} page={page_id}" + (" again=1" if again else "")
                                  + f" attachments={len(atts)}")
                    conn.commit()
                    recorded.update(at=at)
                except Exception as e:  # noqa: BLE001  ページはできているので、登録は止めない
                    conn.rollback()
                    print(f"Notionに登録したことを記録できませんでした (mail={mail_id}): {e}")

            try:
                out = notion.register(s, dict(r), atts, MAIL_ATTACHMENT_DIR, on_created=record)
            except notion.NotionError as e:
                raise HTTPException(502, f"Notionに登録できませんでした: {notify.safe_error(e, s)}")
            except ValueError as e:   # 「列に入れるもの」の書き方の間違い
                raise HTTPException(400, f"設定の「列に入れるもの」を直してください: {e}")
    except notion.NotionBusy as e:
        raise HTTPException(409, str(e))
    warnings = [notify.safe_error(RuntimeError(w), s) for w in out["warnings"]]
    if not recorded:
        warnings.append("登録したことを、このメールに記録できませんでした"
                        "（もう一度押すと、Notionに同じ行がもう1つできます）")
    return {
        "notion_page_url": out["url"],
        "notion_registered_at": recorded.get("at"),
        "notion_registered_by": admin["name"],
        "database_title": out["database_title"],
        "warnings": warnings,
        "attachments_uploaded": out["attachments_uploaded"],
        "attachment_notes": [notify.safe_error(RuntimeError(n), s)
                             for n in out["attachment_notes"]],
        "ai_used": out["ai_used"],
        "ai_error": notify.safe_error(RuntimeError(out["ai_error"]), s) if out["ai_error"] else "",
    }


@app.patch("/api/mail/{mail_id}")
def mail_patch(
    mail_id: int, body: MailPatch, admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """下書きの保存・優先度の手動変更・状態変更 (対応不要など)・種類と担当の変更.

    種類を変えたとき、担当がまだいなければ、その種類の担当 (設定) を入れる。
    返り値には変更後の種類と担当を入れる (画面の選択欄をそろえる用)。
    """
    row = _mail_or_404(conn, mail_id)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    # 担当は null (担当なし) にするのも変更なので、送られてきたかどうかで見る
    if "assignee_user_id" in body.model_fields_set:
        changes["assignee_user_id"] = body.assignee_user_id
    if not changes:
        raise HTTPException(400, "変更内容がありません")
    # 先にすべて確かめてから書き込む
    if "priority" in changes and changes["priority"] not in (1, 2, 3):
        raise HTTPException(400, "priority は 1〜3 で指定してください")
    if "status" in changes and changes["status"] not in mail.MAIL_STATUSES:
        raise HTTPException(400, f"不明な状態です: {changes['status']}")
    s = db.get_settings(conn)
    if "category" in changes:
        category = " ".join(changes["category"].split())
        # 一覧から消した種類のままにしておく (変えない) のは認める
        if category and category != row["category"]:
            found = mail.find_category(category, mail.category_list(s))
            if not found:
                raise HTTPException(
                    400, f"「{category}」は種類の一覧にありません"
                         "（設定の「種類と担当」で追加できます）")
            category = found
        changes["category"] = category
    if "assignee_user_id" in changes:
        uid = changes["assignee_user_id"]
        # 今の担当のままにしておく (後から無効にした人でも) のは認める
        if uid is not None and uid != row["assignee_user_id"] and not conn.execute(
                "SELECT 1 FROM users WHERE id = ? AND active = 1", (uid,)).fetchone():
            raise HTTPException(400, "担当に選んだ人が見つかりません（無効にした人は選べません）")
    elif (changes.get("category") and changes["category"] != row["category"]
          and row["assignee_user_id"] is None):
        # 種類を変えて、担当がまだいないときは、その種類の担当 (設定) を入れる
        uid = mail.default_assignees(conn, s).get(changes["category"])
        if uid is not None:
            changes["assignee_user_id"] = uid

    if "priority" in changes:
        conn.execute(
            "UPDATE mails SET priority = ?, priority_reason = ?,"
            " priority_source = 'manual' WHERE id = ?",
            (changes["priority"], "手動変更", mail_id),
        )
    if "status" in changes:
        conn.execute(
            "UPDATE mails SET status = ? WHERE id = ?",
            (changes["status"], mail_id),
        )
        if changes["status"] == "archived" and row["status"] != "archived":
            # 対応不要にした時刻も残す (週ごとのまとめの「対応不要にした」の数)
            conn.execute("UPDATE mails SET archived_at = ? WHERE id = ?",
                         (now_iso(), mail_id))
    if "draft_reply" in changes:
        conn.execute(
            "UPDATE mails SET draft_reply = ? WHERE id = ?",
            (changes["draft_reply"], mail_id),
        )
    if "category" in changes:
        conn.execute("UPDATE mails SET category = ? WHERE id = ?",
                     (changes["category"], mail_id))
    if "assignee_user_id" in changes:
        conn.execute("UPDATE mails SET assignee_user_id = ? WHERE id = ?",
                     (changes["assignee_user_id"], mail_id))
    r = conn.execute(_MAIL_SELECT + " WHERE m.id = ?", (mail_id,)).fetchone()
    logged = []
    for k, v in changes.items():
        if k == "draft_reply":
            continue
        if k == "category":
            v = v or "未分類"
        elif k == "assignee_user_id":
            # 後から名前を見て分かるよう、番号と一緒に名前も残す
            k, v = "assignee", f"{r['assignee_name']}({v})" if v is not None else "なし"
        logged.append(f"{k}={v}")
    if logged:
        _audit(conn, admin, "mail_update", detail=f"mail={mail_id} " + ", ".join(logged))
    return {"updated": mail_id, "category": r["category"],
            "assignee_user_id": r["assignee_user_id"],
            "assignee_name": r["assignee_name"] or ""}


@app.post("/api/mail/{mail_id}/send")
def mail_send(
    mail_id: int, body: MailSendBody, admin=Depends(require_admin),
    conn=Depends(get_conn),
):
    """返信を送信する。宛先は元メールの差出人・スレッドが繋がるヘッダ付き.

    cc / bcc を渡すと CC・BCC にも送る (全員に返信)。アドレスはここで確かめ、
    おかしなものがあれば何も送らずに 400 を返す。
    """
    r = _mail_or_404(conn, mail_id)
    if r["status"] == "replied":
        raise HTTPException(409, "このメールには返信済みです")
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(400, "返信本文を入力してください")
    if "@" not in (r["from_addr"] or ""):
        raise HTTPException(400, "差出人アドレスが不明なため返信できません")
    try:
        cc, bcc = mail.check_reply_recipients(r["from_addr"], body.cc, body.bcc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    s = db.get_settings(conn)
    if not s.get("smtp_host"):
        raise HTTPException(400, "SMTPが未設定です。設定画面で送信サーバを保存してください")
    subject = (body.subject or "").strip() or mail.reply_subject(r["subject"])
    # 先に「返信済み」へ原子的に更新・コミットしてから送信する (二重送信防止:
    # 同じメールへ同時に送信した場合、後の方は rowcount=0 になり 409 で止まる)。
    # CC・BCC も実際に送ったものとして一緒に残す
    cur = conn.execute(
        "UPDATE mails SET status = 'replied', reply_subject = ?, reply_body = ?,"
        " reply_cc = ?, reply_bcc = ?, replied_at = ?, replied_by = ?"
        " WHERE id = ? AND status != 'replied'",
        (subject, text, ", ".join(cc), ", ".join(bcc), now_iso(), admin["name"],
         mail_id),
    )
    if not cur.rowcount:
        raise HTTPException(409, "このメールには返信済みです")
    conn.commit()

    def back_to_unhandled():
        # 送信できなかったら未対応に戻し、画面から再試行できるようにする
        conn.execute(
            "UPDATE mails SET status = 'unhandled', reply_subject = '',"
            " reply_body = '', reply_cc = '', reply_bcc = '', replied_at = NULL,"
            " replied_by = '', reply_message_id = '' WHERE id = ?",
            (mail_id,),
        )

    refused: list[str] = []
    try:
        raw = mail.send_reply(
            s, r["from_addr"], subject, text,
            in_reply_to=r["message_id"], references=r["references_hdr"],
            cc=cc, bcc=bcc,
        )
    except mail.SomeRecipientsRefused as e:
        # 一部の送り先だけ送信サーバに断られた (ほかの人には届いている)
        raw, refused = e.raw, e.refused
    except Exception as e:  # noqa: BLE001  管理者向けに原因を返す(社内利用)
        back_to_unhandled()
        conn.commit()
        raise HTTPException(502, f"送信に失敗しました: {notify.safe_error(e, s)}")
    refused_keys = {a.strip().lower() for a in refused}
    if r["from_addr"].strip().lower() in refused_keys:
        # 肝心の相手 (元メールの差出人) に届いていない。CC・BCC の人には届いているが、
        # 「返信済み」にすると対応が終わったと思われてしまうので、未対応に戻して知らせる
        delivered = [a for a in (*cc, *bcc) if a.lower() not in refused_keys]
        # CC・BCC の人には届いているので、何を送ったかが分かるよう控えは残す
        # (未対応に戻すと返信文は消え、宛先は画面で直せないので、送り直さずに
        # 閉じたときに、届いた文がどこにも残らなくなる)。
        # 控えの保存 (IMAP) は、DB に書く前にする (書き込みロックを持ったまま待たない)
        saved = ""
        if delivered and raw and mail.should_save_sent(s):
            try:
                sent_folder = mail.save_to_sent(s, raw)
                saved = f" saved={sent_folder}" if sent_folder else ""
            except Exception:  # noqa: BLE001  控えの失敗で知らせ方は変えない
                saved = " saved=failed"
        back_to_unhandled()
        _audit(conn, admin, "mail_send_refused",
               detail=f"to={r['from_addr']} refused={','.join(refused)}"
                      f" delivered={','.join(delivered)} subject={subject}{saved}")
        conn.commit()
        raise HTTPException(
            502, f"送信できませんでした: 宛先（{r['from_addr']}）は相手のメールサーバに"
                 "受け取りを断られました。アドレスを確かめてください"
                 + (f"（CC・BCC の {len(delivered)}件には届いています。"
                    "送り直すと、その人たちには2通目が届きます）" if delivered else ""))
    if refused_keys:
        # CC・BCC の一部だけ届かなかった。「送った CC・BCC」には届いた人だけを残し、
        # 届かなかったアドレスは画面と記録で知らせる
        cc = [a for a in cc if a.lower() not in refused_keys]
        bcc = [a for a in bcc if a.lower() not in refused_keys]
        conn.execute("UPDATE mails SET reply_cc = ?, reply_bcc = ? WHERE id = ?",
                     (", ".join(cc), ", ".join(bcc), mail_id))
        conn.commit()
    # 送った返信の Message-ID を残す (相手がこの返信に返事をくれたとき、別のアドレスからでも
    # 同じやり取りだと分かるように)。控えの保存 (IMAP) の前に確定する (ロックを持ったまま待たない)
    reply_id = mail.sent_message_id(raw)
    if reply_id:
        conn.execute("UPDATE mails SET reply_message_id = ? WHERE id = ?", (reply_id, mail_id))
        conn.commit()
    # 控えを送信済みフォルダへ保存する (Webメールの「送信済み」にも出るように)。
    # 相手には届いているので、ここで失敗しても送信は成功として扱う
    # (失敗扱いにすると画面から再送され、相手に二重に届いてしまう)
    sent_folder, save_error = "", ""
    if raw and mail.should_save_sent(s):
        try:
            sent_folder = mail.save_to_sent(s, raw)
        except Exception as e:  # noqa: BLE001  管理者向けに原因を返す(社内利用)
            save_error = notify.safe_error(e, s)
    # 誰に送ったかを後から確かめられるよう、CC・BCC のアドレスと合計の件数も残す
    detail = f"to={r['from_addr']}"
    if cc:
        detail += f" cc={','.join(cc)}"
    if bcc:
        detail += f" bcc={','.join(bcc)}"
    if refused:
        detail += f" refused={','.join(refused)}"
    detail += f" recipients={1 + len(cc) + len(bcc)} subject={subject}"
    if sent_folder:
        detail += f" saved={sent_folder}"
    elif save_error:
        detail += " saved=failed"
    _audit(conn, admin, "mail_send", detail=detail)
    return {"sent": r["from_addr"], "cc": cc, "bcc": bcc, "refused": refused,
            "subject": subject,
            "saved_to_sent": bool(sent_folder), "sent_folder": sent_folder,
            "sent_save_error": save_error}
