"""月次勤務レポートの集計と CSV 出力 (ローカルタイムゾーン基準)."""
import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone

from . import tz


def _month_sessions(conn: sqlite3.Connection, month: str):
    """当月に少しでも重なるセッションを返す."""
    start, end = tz.month_window(month)
    return conn.execute(
        """
        SELECT user_id, clock_in, clock_out, category FROM sessions
        WHERE clock_in < ? AND (clock_out IS NULL OR clock_out > ?)
        ORDER BY clock_in
        """,
        (tz.utc_iso(end), tz.utc_iso(start)),
    ).fetchall()


def daily_hours_by_user(conn: sqlite3.Connection, month: str) -> dict:
    """{user_id: {date: 在席時間}} を返す (日跨ぎはローカル日付で分割)."""
    start, end = tz.month_window(month)
    now = datetime.now(timezone.utc)
    res: dict[int, dict[str, float]] = {}
    for s in _month_sessions(conn, month):
        seg_start = max(tz.local(s["clock_in"]), start)
        seg_close = min(
            tz.local(s["clock_out"]) if s["clock_out"] else now.astimezone(tz.TZ), end
        )
        while seg_start < seg_close:
            day_end = (seg_start + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seg_end = min(day_end, seg_close)
            d = res.setdefault(s["user_id"], {})
            key = seg_start.date().isoformat()
            d[key] = d.get(key, 0.0) + (seg_end - seg_start).total_seconds() / 3600
            seg_start = seg_end
    return res


def monthly_report(
    conn: sqlite3.Connection, month: str, target_hours: float = 8.0
) -> list[dict]:
    """month は 'YYYY-MM'。ユーザーごとに勤務日数・合計時間・残業/不足を集計する.

    終了していないセッションは現在時刻までで計上する。日付・月の境界は
    ローカルタイムゾーン (ZATSUMU_TZ) で判定する。残業=各勤務日で予定時間を
    超えた分の合計、不足=勤務日で予定時間に満たない分の合計。
    """
    daily = daily_hours_by_user(conn, month)  # validates format
    counts: dict[int, int] = {}
    for s in _month_sessions(conn, month):
        start, end = tz.month_window(month)
        if tz.overlap_hours(s["clock_in"], s["clock_out"], start, end,
                            datetime.now(timezone.utc)) > 0:
            counts[s["user_id"]] = counts.get(s["user_id"], 0) + 1

    out = []
    for r in conn.execute("SELECT id, name FROM users ORDER BY name"):
        days = daily.get(r["id"], {})
        worked = [h for h in days.values() if h > 0]
        total = sum(worked)
        overtime = sum(max(0.0, h - target_hours) for h in worked)
        shortfall = sum(max(0.0, target_hours - h) for h in worked)
        out.append({
            "user_id": r["id"],
            "name": r["name"],
            "work_days": len(worked),
            "sessions": counts.get(r["id"], 0),
            "total_hours": round(total, 2),
            "target_hours": round(len(worked) * target_hours, 2),
            "overtime": round(overtime, 2),
            "shortfall": round(shortfall, 2),
        })
    return out


def report_to_csv(report: list[dict], month: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月", "メンバー", "勤務日数", "セッション数", "合計勤務時間(h)",
                     "予定時間(h)", "残業(h)", "不足(h)"])
    for r in report:
        writer.writerow(
            [month, r["name"], r["work_days"], r["sessions"], r["total_hours"],
             r["target_hours"], r["overtime"], r["shortfall"]]
        )
    return buf.getvalue()


def daily_csv(conn: sqlite3.Connection, month: str) -> str:
    """日別集計: 日付 × メンバーの在席時間マトリクス (給与計算用)."""
    start, end = tz.month_window(month)
    users = conn.execute(
        "SELECT id, name FROM users WHERE is_admin = 0 OR id IN "
        "(SELECT DISTINCT user_id FROM sessions) ORDER BY name"
    ).fetchall()
    by_user = daily_hours_by_user(conn, month)
    # hours[date][user_id]
    hours: dict[str, dict[int, float]] = {}
    for uid, days in by_user.items():
        for key, h in days.items():
            hours.setdefault(key, {})[uid] = h

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["日付"] + [u["name"] for u in users])
    day = start
    while day < end:
        key = day.date().isoformat()
        row = hours.get(key, {})
        writer.writerow(
            [key] + [round(row[u["id"]], 2) if u["id"] in row else "" for u in users]
        )
        day += timedelta(days=1)
    return buf.getvalue()


def sessions_csv(conn: sqlite3.Connection, month: str) -> str:
    """在席データ: 当月の全打刻 (着席/退席) を生データで出力する."""
    tz.month_window(month)  # validate
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["メンバー", "作業区分", "着席", "退席"])
    for s in _month_sessions(conn, month):
        writer.writerow(
            [
                names.get(s["user_id"], s["user_id"]),
                s["category"] or "",
                tz.local(s["clock_in"]).strftime("%Y-%m-%d %H:%M:%S"),
                tz.local(s["clock_out"]).strftime("%Y-%m-%d %H:%M:%S")
                if s["clock_out"]
                else "勤務中",
            ]
        )
    return buf.getvalue()
