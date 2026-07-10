"""zatsumu server — テレワーク勤怠・稼働可視化 MVP (F-Chair+ 風)."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from . import db, imaging, notify, reports, retention, tz

DATA_DIR = Path(os.environ.get("ZATSUMU_DATA_DIR", db.DB_PATH.parent))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
# バックグラウンド処理(キャプチャ自動削除・長時間在席チェック)の実行間隔(分)
ALERT_CHECK_INTERVAL_MIN = float(os.environ.get("ZATSUMU_CHECK_INTERVAL_MIN", "10"))


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(DATA_DIR / "zatsumu.db")
    try:
        if os.environ.get("ZATSUMU_DEMO") == "1":
            from . import demo

            if demo.seed(conn, SCREENSHOT_DIR):
                print("デモデータを投入しました (管理者トークン: demo-admin)")
        # 保存済みのタイムゾーン設定を適用する
        tz.set_tz(db.get_settings(conn)["timezone"])
    finally:
        conn.close()
    task = asyncio.create_task(_background_loop())
    yield
    task.cancel()


app = FastAPI(title="zatsumu", version="0.1.0", lifespan=lifespan)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    conn = db.connect(DATA_DIR / "zatsumu.db")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def auth_user(authorization: str = Header(None), conn=Depends(get_conn)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    user = db.user_by_token(conn, authorization.removeprefix("Bearer "))
    if not user:
        raise HTTPException(401, "Invalid token")
    return user


def require_admin(user=Depends(auth_user)):
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


@app.get("/api/settings")
def get_settings_api(_admin=Depends(require_admin), conn=Depends(get_conn)):
    s = db.get_settings(conn)
    # SMTPパスワードはレスポンスに含めない(設定済みかだけ返す)
    s["smtp_pass_set"] = bool(s.get("smtp_pass"))
    s["smtp_pass"] = ""
    return s


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
    }
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "変更内容がありません")
    int_changes = {k: int(v) for k, v in changes.items() if k in db.INT_SETTINGS}
    merged = {**db.get_settings(conn), **int_changes}
    for key, val in int_changes.items():
        lo, hi = limits[key]
        if not lo <= val <= hi:
            raise HTTPException(400, f"{key} は {lo}〜{hi} の範囲で指定してください")
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
    if "smtp_port" in changes and str(changes["smtp_port"]).strip():
        port = str(changes["smtp_port"]).strip()
        if not (port.isdigit() and 1 <= int(port) <= 65535):
            raise HTTPException(400, "SMTPポートは1〜65535の数値で指定してください")
        changes["smtp_port"] = port
    if "work_categories" in changes:
        cats = [c.strip() for c in changes["work_categories"].split(",") if c.strip()]
        if not cats:
            raise HTTPException(400, "作業区分を1つ以上入力してください")
        changes["work_categories"] = ",".join(cats)

    for key, val in changes.items():
        db.set_setting(conn, key, val)
    secret = {"slack_webhook_url", "smtp_pass", "smtp_user"}
    _audit(conn, admin, "settings_update", detail=", ".join(
        f"{k}=***" if k in secret else f"{k}={v}" for k, v in changes.items()))
    return db.get_settings(conn)


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
        raise HTTPException(502, f"送信に失敗しました: {e}")
    return {"sent": to}


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
    for tbl in ("screenshots", "sessions", "journals", "leave_requests", "corrections"):
        conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (user_id,))
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
    return {"status": "ok"}


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
