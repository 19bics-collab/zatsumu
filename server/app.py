"""zatsumu server — テレワーク勤怠・稼働可視化 MVP (F-Chair+ 風)."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from . import db, reports, retention, tz

DATA_DIR = Path(os.environ.get("ZATSUMU_DATA_DIR", db.DB_PATH.parent))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
# 自動削除の実行間隔(時間)。保存日数自体は設定(retention_days)で管理する
PURGE_INTERVAL_HOURS = float(os.environ.get("ZATSUMU_PURGE_INTERVAL_HOURS", "6"))


async def _purge_loop() -> None:
    while True:
        conn = db.connect(DATA_DIR / "zatsumu.db")
        try:
            days = db.get_settings(conn)["retention_days"]
            if days > 0:
                retention.purge_old_screenshots(conn, SCREENSHOT_DIR, days)
        finally:
            conn.close()
        await asyncio.sleep(PURGE_INTERVAL_HOURS * 3600)


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
    task = asyncio.create_task(_purge_loop())
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


class SettingsPatch(BaseModel):
    capture_min_interval: int | None = None
    capture_max_interval: int | None = None
    capture_quality: int | None = None
    capture_blur: int | None = None
    capture_enabled: bool | None = None
    retention_days: int | None = None
    alert_hours: int | None = None
    company_name: str | None = None
    timezone: str | None = None
    work_start: str | None = None
    work_end: str | None = None
    work_categories: str | None = None


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
    return {"session_id": session["id"], "clock_out": now_iso()}


@app.post("/api/screenshots")
async def upload_screenshot(
    image: UploadFile = File(...), user=Depends(auth_user), conn=Depends(get_conn)
):
    if not db.open_session(conn, user["id"]):
        raise HTTPException(409, "Not clocked in")
    taken_at = datetime.now(timezone.utc)
    rel = f"{user['id']}/{taken_at.strftime('%Y%m%d_%H%M%S')}.jpg"
    dest = SCREENSHOT_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await image.read())
    cur = conn.execute(
        "INSERT INTO screenshots (user_id, taken_at, path) VALUES (?, ?, ?)",
        (user["id"], taken_at.isoformat(), rel),
    )
    return {"screenshot_id": cur.lastrowid}


@app.get("/api/status")
def status(_admin=Depends(require_admin), conn=Depends(get_conn)):
    now = datetime.now(timezone.utc)
    day_start, _ = tz.today_window(now)
    rows = conn.execute(
        """
        SELECT u.id, u.name,
               s.clock_in AS open_since,
               s.category AS open_category,
               (SELECT taken_at FROM screenshots WHERE user_id = u.id
                ORDER BY taken_at DESC LIMIT 1) AS last_screenshot
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id AND s.clock_out IS NULL
        ORDER BY u.name
        """
    ).fetchall()
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
    return [
        {
            "user_id": r["id"],
            "name": r["name"],
            "seated": r["open_since"] is not None,
            "open_since": r["open_since"],
            "category": r["open_category"],
            "hours_today": round(hours.get(r["id"], 0.0), 2),
            "last_screenshot": r["last_screenshot"],
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
        # 日をまたぐセッションはローカル日付ごとの区間に分割する
        seg_start = max(tz.local(s["clock_in"]), start)
        seg_close = min(
            tz.local(s["clock_out"]) if s["clock_out"] else now.astimezone(tz.TZ), end
        )
        while seg_start < seg_close:
            day_end = (seg_start + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seg_end = min(day_end, seg_close)
            day = day_of(seg_start)
            day["sessions"].append(
                {
                    "id": s["id"],
                    "start": seg_start.isoformat(),
                    "end": seg_end.isoformat(),
                    "open": s["clock_out"] is None and seg_end == seg_close,
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
            seg_start = seg_end

    shots = conn.execute(
        "SELECT id, taken_at FROM screenshots WHERE user_id = ? "
        "AND taken_at >= ? AND taken_at < ? ORDER BY taken_at",
        (user_id, tz.utc_iso(start), tz.utc_iso(end)),
    ).fetchall()
    for sh in shots:
        t = tz.local(sh["taken_at"])
        day_of(t)["screenshots"].append({"id": sh["id"], "taken_at": t.isoformat()})

    journ = conn.execute(
        "SELECT date FROM journals WHERE user_id = ? AND date >= ? AND date < ?",
        (user_id, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    ).fetchall()
    journal_days = {r["date"] for r in journ}

    for key, d in days.items():
        d["hours"] = round(d["hours"], 2)
    for jd in journal_days:  # 在席が無くても日報がある日を含める
        day_of(datetime.strptime(jd, "%Y-%m-%d").replace(tzinfo=tz.TZ))
    for key, d in days.items():
        d["has_journal"] = key in journal_days
    return {
        "user": {"id": user["id"], "name": user["name"]},
        "month": month,
        "days": sorted(days.values(), key=lambda d: d["date"]),
        "categories": db.work_categories(conn),
        "by_category": {k: round(v, 2) for k, v in by_category.items()},
    }


@app.get("/api/settings")
def get_settings_api(_admin=Depends(require_admin), conn=Depends(get_conn)):
    return db.get_settings(conn)


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
    for key in ("work_start", "work_end"):
        if key in changes:
            try:
                datetime.strptime(changes[key], "%H:%M")
            except ValueError:
                raise HTTPException(400, f"{key} は HH:MM 形式で入力してください")
    if "timezone" in changes and not tz.set_tz(changes["timezone"]):
        raise HTTPException(400, "不明なタイムゾーンです (例: Asia/Tokyo)")
    if "work_categories" in changes:
        cats = [c.strip() for c in changes["work_categories"].split(",") if c.strip()]
        if not cats:
            raise HTTPException(400, "作業区分を1つ以上入力してください")
        changes["work_categories"] = ",".join(cats)

    for key, val in changes.items():
        db.set_setting(conn, key, val)
    _audit(conn, admin, "settings_update",
           detail=", ".join(f"{k}={v}" for k, v in changes.items()))
    return db.get_settings(conn)


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
            "SELECT id, name, is_admin, active, capture_enabled "
            "FROM users ORDER BY name"
        )
    ]


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
    row = conn.execute(
        "SELECT id, name, is_admin, active, capture_enabled FROM users "
        "WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row)


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
        conn.execute(
            "DELETE FROM journals WHERE user_id = ? AND date = ?",
            (user["id"], date),
        )
        return {"date": date, "body": ""}
    conn.execute(
        "INSERT INTO journals (user_id, date, body, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET body = excluded.body, "
        "updated_at = excluded.updated_at",
        (user["id"], date, text, now_iso()),
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
    return [dict(r) for r in conn.execute(q, args).fetchall()]


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


def _validate_month(month: str | None) -> str:
    month = month or datetime.now(tz.TZ).strftime("%Y-%m")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(400, "month must be 'YYYY-MM'")
    return month


@app.get("/api/reports/monthly")
def monthly_report(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    month = _validate_month(month)
    return {"month": month, "rows": reports.monthly_report(conn, month)}


@app.get("/api/reports/monthly.csv", response_class=PlainTextResponse)
def monthly_report_csv(
    month: str | None = None, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    month = _validate_month(month)
    csv_text = reports.report_to_csv(reports.monthly_report(conn, month), month)
    return Response(
        content="﻿" + csv_text,  # Excel 用 BOM
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="zatsumu_{month}.csv"'
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
        "token_regen": "トークン再発行",
        "force_clock_out": "強制退席",
        "session_add": "打刻追加",
        "session_edit": "打刻修正",
        "session_delete": "打刻削除",
        "settings_update": "設定変更",
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
