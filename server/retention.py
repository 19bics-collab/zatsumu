"""スクリーンショットの保存期間管理.

指定日数より古いスクリーンショットを DB レコードと画像ファイルごと削除する。
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def purge_old_screenshots(
    conn: sqlite3.Connection, screenshot_dir: Path, retention_days: int
) -> int:
    """retention_days より古いスクショを削除し、削除件数を返す."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    rows = conn.execute(
        "SELECT id, path FROM screenshots WHERE taken_at < ?", (cutoff,)
    ).fetchall()
    for row in rows:
        file = screenshot_dir / row["path"]
        file.unlink(missing_ok=True)
    if rows:
        conn.executemany(
            "DELETE FROM screenshots WHERE id = ?", [(r["id"],) for r in rows]
        )
        conn.commit()
    return len(rows)
