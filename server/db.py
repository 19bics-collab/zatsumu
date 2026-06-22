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
CREATE TABLE IF NOT EXISTS journals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, date)
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    leave_type TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER,
    UNIQUE(user_id, date)
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,            -- 修正対象の日 (YYYY-MM-DD ローカル)
    requested_in TEXT,            -- 希望の着席時刻 "HH:MM" (任意)
    requested_out TEXT,           -- 希望の退席時刻 "HH:MM" (任意)
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);
-- 集計・参照でよく使う列のインデックス(IF NOT EXISTS で冪等)
CREATE INDEX IF NOT EXISTS idx_sessions_user_open ON sessions(user_id, clock_out);
CREATE INDEX IF NOT EXISTS idx_sessions_clock_in ON sessions(clock_in);
CREATE INDEX IF NOT EXISTS idx_screenshots_user_taken ON screenshots(user_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_screenshots_taken ON screenshots(taken_at);
CREATE INDEX IF NOT EXISTS idx_leave_date ON leave_requests(date);
CREATE INDEX IF NOT EXISTS idx_corrections_status ON corrections(status);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""

# 休暇の種別
LEAVE_TYPES = ["有給休暇", "半休", "欠勤", "特別休暇", "その他"]

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
    "daily_target_minutes": 480,   # 1日の予定勤務時間(分) 既定8時間
    "notify_clock": 0,             # 着席/退席を通知するか
    "notify_alert": 1,             # 長時間在席を通知するか
    "notify_stall": 1,             # 画面が変化しない(停滞)場合に通知するか
    "stall_threshold": 95,         # 直前のキャプチャとの一致率がこの%以上で「同じ画面」
    "stall_alert_count": 3,        # 同じ画面が連続でこの回数続いたら通知
    "clockout_reminder": 1,        # 終業時刻を過ぎても未退勤の本人へリマインドするか
    "idle_threshold": 120,         # 無操作がこの秒数以上なら「離席/非稼働」とみなす(稼働率計算用)
}

# 全社設定の既定値 (文字列)
STR_SETTINGS = {
    "company_name": "zatsumu",     # ヘッダー等に表示する会社名/サービス名
    "timezone": os.environ.get("ZATSUMU_TZ", "Asia/Tokyo"),  # 集計の基準TZ
    "work_start": "09:00",         # 勤務時間帯の目安(開始) タイムライン表示用
    "work_end": "18:00",           # 勤務時間帯の目安(終了)
    "clockout_reminder_time": "20:00",  # この時刻以降、未退勤の本人へ退勤リマインド
    "work_categories": "事務作業,現場",  # 作業区分(カンマ区切り、先頭が既定)
    "slack_webhook_url": "",       # Slack Incoming Webhook URL
    "mail_to": "",                 # 通知メール宛先(カンマ区切り)
    "smtp_host": "",               # SMTPサーバ (空ならメール無効)
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_pass": "",
    "mail_from": "",               # 差出人 (空なら smtp_user を使用)
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
    if "notify_enabled" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN notify_enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "team_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN team_id INTEGER")
    shcols = [r["name"] for r in conn.execute("PRAGMA table_info(screenshots)")]
    if "sig" not in shcols:
        conn.execute("ALTER TABLE screenshots ADD COLUMN sig TEXT")
    if "similarity" not in shcols:  # 直前のキャプチャとの一致率(%)
        conn.execute("ALTER TABLE screenshots ADD COLUMN similarity INTEGER")
    if "stall" not in shcols:       # 同じ画面が連続した回数
        conn.execute(
            "ALTER TABLE screenshots ADD COLUMN stall INTEGER NOT NULL DEFAULT 0"
        )
    if "idle" not in shcols:        # 撮影時点の無操作秒数 (null=未取得/Web撮影)
        conn.execute("ALTER TABLE screenshots ADD COLUMN idle INTEGER")
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
    if "category" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN category TEXT")
    if "alert_notified" not in scols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN alert_notified INTEGER NOT NULL DEFAULT 0"
        )
    if "clockout_reminded" not in scols:  # 退勤リマインド済みフラグ(重複通知防止)
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN clockout_reminded INTEGER NOT NULL DEFAULT 0"
        )
    # team_id 列が用意できた後にインデックスを作成する(SCHEMA時点では未追加のため)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id)")


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
