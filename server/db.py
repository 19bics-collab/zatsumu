"""SQLite helpers for the zatsumu server."""
import os
import sqlite3
import secrets
from contextlib import contextmanager
from pathlib import Path

DB_PATH = (
    Path(
        os.environ.get("ZATSUMU_DATA_DIR")
        or Path(__file__).resolve().parent.parent / "data"
    )
    / "zatsumu.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    clock_in TEXT NOT NULL,
    clock_out TEXT
);
CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    taken_at TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_user_id INTEGER,
    session_id INTEGER,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 全社設定の既定値 (整数)。settings テーブルの値で上書きされる
INT_SETTINGS = {
    "capture_min_interval": 300,   # 撮影の最短間隔(秒)
    "capture_max_interval": 900,   # 撮影の最長間隔(秒) 平均10分=約6回/時
    "capture_quality": 60,         # JPEG品質 (10-95)
    "capture_blur": 0,             # ぼかし強度 (0=なし)
    "capture_enabled": 1,          # 全社の撮影ON/OFF
    # キャプチャ保存日数 (0=自動削除なし)。環境変数は初期値として機能する
    "retention_days": int(os.environ.get("ZATSUMU_RETENTION_DAYS", "30")),
    "alert_hours": 6,              # 連続在席アラート(時間)
}

# 全社設定の既定値 (文字列)
STR_SETTINGS = {
    "company_name": "zatsumu",     # ヘッダー等に表示する会社名/サービス名
    "timezone": os.environ.get("ZATSUMU_TZ", "Asia/Tokyo"),  # 集計の基準TZ
    "work_start": "09:00",         # 勤務時間帯の目安(開始) タイムライン表示用
    "work_end": "18:00",           # 勤務時間帯の目安(終了)
    "work_categories": "事務作業,現場",  # 作業区分(カンマ区切り、先頭が既定)
}

# 後方互換: 旧名を参照しているコード向け
SETTING_DEFAULTS = INT_SETTINGS


def get_settings(conn: sqlite3.Connection) -> dict:
    stored = {
        r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")
    }
    out: dict = {
        k: int(stored.get(k, default)) for k, default in INT_SETTINGS.items()
    }
    out.update(
        {k: stored.get(k, default) for k, default in STR_SETTINGS.items()}
    )
    return out


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(int(value)) if key in INT_SETTINGS else str(value)),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBへの後方互換マイグレーション."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    if "active" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
        )
    if "capture_enabled" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN capture_enabled INTEGER NOT NULL DEFAULT 1"
        )
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
    if "category" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN category TEXT")


def work_categories(conn: sqlite3.Connection) -> list[str]:
    """設定された作業区分のリスト (先頭が既定)."""
    raw = get_settings(conn)["work_categories"]
    cats = [c.strip() for c in raw.split(",") if c.strip()]
    return cats or ["事務作業"]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI runs sync deps in a threadpool but async endpoints on the
    # event loop thread, so the same connection crosses threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def get_db(db_path: Path | str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_user(conn: sqlite3.Connection, name: str, is_admin: bool = False) -> dict:
    token = secrets.token_urlsafe(24)
    cur = conn.execute(
        "INSERT INTO users (name, token, is_admin) VALUES (?, ?, ?)",
        (name, token, int(is_admin)),
    )
    return {"id": cur.lastrowid, "name": name, "token": token, "is_admin": is_admin}


def user_by_token(conn: sqlite3.Connection, token: str):
    return conn.execute(
        "SELECT * FROM users WHERE token = ? AND active = 1", (token,)
    ).fetchone()


def open_session(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND clock_out IS NULL "
        "ORDER BY clock_in DESC LIMIT 1",
        (user_id,),
    ).fetchone()
