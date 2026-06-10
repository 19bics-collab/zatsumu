"""月次勤務レポートの集計と CSV 出力."""
import csv
import io
import sqlite3
from datetime import datetime, timezone


def monthly_report(conn: sqlite3.Connection, month: str) -> list[dict]:
    """month は 'YYYY-MM'。ユーザーごとに勤務日数・合計時間を集計する.

    終了していないセッション(clock_out IS NULL)は現在時刻までで計上する。
    """
    datetime.strptime(month, "%Y-%m")  # validate format, raises ValueError
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """
        SELECT u.id AS user_id, u.name,
               COUNT(s.id) AS sessions,
               COUNT(DISTINCT date(s.clock_in)) AS work_days,
               COALESCE(SUM(
                   (julianday(COALESCE(s.clock_out, ?)) - julianday(s.clock_in)) * 24
               ), 0) AS total_hours
        FROM users u
        LEFT JOIN sessions s
            ON s.user_id = u.id AND strftime('%Y-%m', s.clock_in) = ?
        GROUP BY u.id, u.name
        ORDER BY u.name
        """,
        (now, month),
    ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "name": r["name"],
            "work_days": r["work_days"],
            "sessions": r["sessions"],
            "total_hours": round(r["total_hours"], 2),
        }
        for r in rows
    ]


def report_to_csv(report: list[dict], month: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月", "メンバー", "勤務日数", "セッション数", "合計勤務時間(h)"])
    for r in report:
        writer.writerow(
            [month, r["name"], r["work_days"], r["sessions"], r["total_hours"]]
        )
    return buf.getvalue()
