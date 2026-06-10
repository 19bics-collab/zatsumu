"""SQLite helpers for the zatsumu server."""
import sqlite3
import secrets
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "zatsumu.db"

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
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI runs sync deps in a threadpool but async endpoints on the
    # event loop thread, so the same connection crosses threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
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
    return conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()


def open_session(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND clock_out IS NULL "
        "ORDER BY clock_in DESC LIMIT 1",
        (user_id,),
    ).fetchone()
