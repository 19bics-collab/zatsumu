"""zatsumu server — テレワーク勤怠・稼働可視化 MVP (F-Chair+ 風)."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from . import db, reports, retention

DATA_DIR = Path(os.environ.get("ZATSUMU_DATA_DIR", db.DB_PATH.parent))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
# スクショ保存日数。0 以下で自動削除を無効化。
RETENTION_DAYS = int(os.environ.get("ZATSUMU_RETENTION_DAYS", "30"))
# 自動削除の実行間隔(時間)。
PURGE_INTERVAL_HOURS = float(os.environ.get("ZATSUMU_PURGE_INTERVAL_HOURS", "6"))


async def _purge_loop() -> None:
    while True:
        conn = db.connect(DATA_DIR / "zatsumu.db")
        try:
            retention.purge_old_screenshots(conn, SCREENSHOT_DIR, RETENTION_DAYS)
        finally:
            conn.close()
        await asyncio.sleep(PURGE_INTERVAL_HOURS * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_purge_loop()) if RETENTION_DAYS > 0 else None
    yield
    if task:
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


@app.post("/api/clock-in")
def clock_in(user=Depends(auth_user), conn=Depends(get_conn)):
    if db.open_session(conn, user["id"]):
        raise HTTPException(409, "Already clocked in")
    cur = conn.execute(
        "INSERT INTO sessions (user_id, clock_in) VALUES (?, ?)",
        (user["id"], now_iso()),
    )
    return {"session_id": cur.lastrowid, "clock_in": now_iso()}


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
    rows = conn.execute(
        """
        SELECT u.id, u.name,
               s.clock_in AS open_since,
               (SELECT COALESCE(SUM(
                    (julianday(COALESCE(clock_out, ?)) - julianday(clock_in)) * 24
                ), 0) FROM sessions
                WHERE user_id = u.id AND date(clock_in) = date(?)) AS hours_today,
               (SELECT taken_at FROM screenshots WHERE user_id = u.id
                ORDER BY taken_at DESC LIMIT 1) AS last_screenshot
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id AND s.clock_out IS NULL
        ORDER BY u.name
        """,
        (now_iso(), now_iso()),
    ).fetchall()
    return [
        {
            "user_id": r["id"],
            "name": r["name"],
            "seated": r["open_since"] is not None,
            "open_since": r["open_since"],
            "hours_today": round(r["hours_today"], 2),
            "last_screenshot": r["last_screenshot"],
        }
        for r in rows
    ]


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
    screenshot_id: int, _admin=Depends(require_admin), conn=Depends(get_conn)
):
    row = conn.execute(
        "SELECT path FROM screenshots WHERE id = ?", (screenshot_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return FileResponse(SCREENSHOT_DIR / row["path"], media_type="image/jpeg")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    # 静的ページ。データは全て Bearer 認証付き API 経由で取得する。
    return (Path(__file__).parent / "templates" / "admin.html").read_text(
        encoding="utf-8"
    )


def _validate_month(month: str | None) -> str:
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
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


@app.post("/api/admin/purge")
def purge_now(_admin=Depends(require_admin), conn=Depends(get_conn)):
    deleted = retention.purge_old_screenshots(conn, SCREENSHOT_DIR, RETENTION_DAYS)
    return {"deleted": deleted, "retention_days": RETENTION_DAYS}
