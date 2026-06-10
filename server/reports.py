"""月次勤務レポートの集計と CSV 出力 (ローカルタイムゾーン基準)."""
import csv
import io
import sqlite3
from datetime import datetime, timezone

from . import tz


def _month_sessions(conn: sqlite3.Connection, month: str):
    """当月に少しでも重なるセッションを返す."""
    start, end = tz.month_window(month)
    return conn.execute(
        """
        SELECT user_id, clock_in, clock_out FROM sessions
        WHERE clock_in < ? AND (clock_out IS NULL OR clock_out > ?)
        ORDER BY clock_in
        """,
        (tz.utc_iso(end), tz.utc_iso(start)),
    ).fetchall()


def monthly_report(conn: sqlite3.Connection, month: str) -> list[dict]:
    """month は 'YYYY-MM'。ユーザーごとに勤務日数・合計時間を集計する.

    終了していないセッションは現在時刻までで計上する。日付・月の境界は
    ローカルタイムゾーン (ZATSUMU_TZ) で判定する。
    """
    start, end = tz.month_window(month)  # validates format, raises ValueError
    now = datetime.now(timezone.utc)
    stats: dict[int, dict] = {}
    for s in _month_sessions(conn, month):
        hours = tz.overlap_hours(s["clock_in"], s["clock_out"], start, end, now)
        if hours <= 0:
            continue
        st = stats.setdefault(s["user_id"], {"hours": 0.0, "days": set(), "n": 0})
        st["hours"] += hours
        st["days"].add(max(tz.local(s["clock_in"]), start).date())
        st["n"] += 1

    rows = conn.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    return [
        {
            "user_id": r["id"],
            "name": r["name"],
            "work_days": len(stats[r["id"]]["days"]) if r["id"] in stats else 0,
            "sessions": stats[r["id"]]["n"] if r["id"] in stats else 0,
            "total_hours": round(stats[r["id"]]["hours"], 2) if r["id"] in stats else 0,
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


def sessions_csv(conn: sqlite3.Connection, month: str) -> str:
    """在席データ: 当月の全打刻 (着席/退席) を生データで出力する."""
    tz.month_window(month)  # validate
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["メンバー", "着席", "退席"])
    for s in _month_sessions(conn, month):
        writer.writerow(
            [
                names.get(s["user_id"], s["user_id"]),
                tz.local(s["clock_in"]).strftime("%Y-%m-%d %H:%M:%S"),
                tz.local(s["clock_out"]).strftime("%Y-%m-%d %H:%M:%S")
                if s["clock_out"]
                else "勤務中",
            ]
        )
    return buf.getvalue()
