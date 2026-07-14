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
        for seg_start, seg_end, _open in tz.day_segments(
            s["clock_in"], s["clock_out"], start, end, now
        ):
            d = res.setdefault(s["user_id"], {})
            key = seg_start.date().isoformat()
            d[key] = d.get(key, 0.0) + (seg_end - seg_start).total_seconds() / 3600
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
    start, end = tz.month_window(month)
    now = datetime.now(timezone.utc)
    counts: dict[int, int] = {}
    for s in _month_sessions(conn, month):
        if tz.overlap_hours(s["clock_in"], s["clock_out"], start, end, now) > 0:
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


def summary(conn: sqlite3.Connection, month: str) -> dict:
    """集計グラフ用: 日別の総労働時間・作業区分別・チーム別 (全社, ローカル日付)."""
    start, end = tz.month_window(month)
    now = datetime.now(timezone.utc)
    team_of = {r["id"]: r["team_id"] for r in conn.execute("SELECT id, team_id FROM users")}
    team_name = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM teams")}
    daily: dict[str, float] = {}
    by_cat: dict[str, float] = {}
    by_team: dict[str, float] = {}
    for s in _month_sessions(conn, month):
        cat = s["category"] or "未分類"
        tname = team_name.get(team_of.get(s["user_id"]), "未所属")
        for seg_start, seg_end, _open in tz.day_segments(
            s["clock_in"], s["clock_out"], start, end, now
        ):
            h = (seg_end - seg_start).total_seconds() / 3600
            key = seg_start.date().isoformat()
            daily[key] = daily.get(key, 0.0) + h
            by_cat[cat] = by_cat.get(cat, 0.0) + h
            by_team[tname] = by_team.get(tname, 0.0) + h
    days = []
    day = start
    while day < end:
        key = day.date().isoformat()
        days.append({"date": key, "hours": round(daily.get(key, 0.0), 2)})
        day += timedelta(days=1)
    return {
        "daily": days,
        "by_category": {k: round(v, 2) for k, v in by_cat.items()},
        "by_team": sorted(
            [{"team": k, "hours": round(v, 2)} for k, v in by_team.items()],
            key=lambda x: -x["hours"],
        ),
    }


def _hhmm(hours: float) -> str:
    """小数の時間を 時:分 表記にする (給与ソフト取込用)."""
    m = round(hours * 60)
    return f"{m // 60}:{m % 60:02d}"


def payroll_csv(conn: sqlite3.Connection, month: str, target_hours: float = 8.0) -> str:
    """給与ソフト取込用CSV: 社員ごとの当月実績 (時:分 併記)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["年月", "社員名", "勤務日数", "総労働時間(h)", "総労働時間(時:分)",
                     "所定時間(h)", "残業(h)", "残業(時:分)"])
    for r in monthly_report(conn, month, target_hours):
        if r["work_days"] == 0:
            continue
        writer.writerow([
            month, r["name"], r["work_days"],
            r["total_hours"], _hhmm(r["total_hours"]),
            r["target_hours"], r["overtime"], _hhmm(r["overtime"]),
        ])
    return buf.getvalue()


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
    writer.writerow(["日付"] + [u["name"] for u in users] + ["合計"])
    member_totals = {u["id"]: 0.0 for u in users}
    day = start
    while day < end:
        key = day.date().isoformat()
        row = hours.get(key, {})
        # 各セルを丸め、右端の「合計」は表示セルの和にして表と一致させる。
        # 丸めて 0 になるセルは表示せず(空欄)、合計とゼロ扱いを揃える。
        cells = {u["id"]: round(row[u["id"]], 2) for u in users
                 if u["id"] in row and round(row[u["id"]], 2) > 0}
        vals = [cells.get(u["id"], "") for u in users]
        day_total = round(sum(cells.values()), 2)
        for uid, h in cells.items():
            member_totals[uid] += h
        writer.writerow([key] + vals + [day_total if cells else ""])
        day += timedelta(days=1)
    # 末尾に各メンバーの月合計行 (最右は総合計)
    mvals = [round(member_totals[u["id"]], 2) if member_totals[u["id"]] else ""
             for u in users]
    grand = round(sum(member_totals.values()), 2)
    writer.writerow(["合計"] + mvals + [grand])
    return buf.getvalue()


def _seg_hours_by_date_user_cat(conn: sqlite3.Connection, month: str) -> dict:
    """{(date, user_id, category): 在席時間} を集計 (日跨ぎはローカル日付で分割)."""
    start, end = tz.month_window(month)
    now = datetime.now(timezone.utc)
    agg: dict[tuple, float] = {}
    seen_cats: set[str] = set()
    for s in _month_sessions(conn, month):
        cat = s["category"] or "未分類"
        seen_cats.add(cat)
        for seg_start, seg_end, _open in tz.day_segments(
            s["clock_in"], s["clock_out"], start, end, now
        ):
            h = (seg_end - seg_start).total_seconds() / 3600
            key = (seg_start.date().isoformat(), s["user_id"], cat)
            agg[key] = agg.get(key, 0.0) + h
    return agg, seen_cats


def _ordered_categories(conn: sqlite3.Connection, seen: set) -> list:
    """列順: 設定の作業区分順 → データにしかない区分 → 未分類 を最後に."""
    from . import db
    configured = db.work_categories(conn)
    # 設定が重複区分名を含んでも列が重複しないよう順序保持で dedup する
    ordered = [c for c in dict.fromkeys(configured) if c in seen]
    extras = sorted(c for c in seen if c not in configured and c != "未分類")
    return ordered + extras + (["未分類"] if "未分類" in seen else [])


def daily_by_category_csv(conn: sqlite3.Connection, month: str) -> str:
    """日別×区分: 日付・メンバー・作業区分ごとの在席時間 (縦持ち・ピボット向き)."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}
    agg, _ = _seg_hours_by_date_user_cat(conn, month)
    # (日付, メンバー) ごとの当日合計 (全区分の合算) を右端に添える。
    # 各区分セルは丸めて表示するので、当日合計も丸め済みセルの和にして表と一致させる。
    day_member: dict[tuple, float] = {}
    for (date, uid, cat), h in agg.items():
        day_member[(date, uid)] = day_member.get((date, uid), 0.0) + round(h, 2)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["日付", "メンバー", "作業区分", "在席時間(h)", "在席時間(時:分)", "当日合計(h)"]
    )
    for date, uid, cat in sorted(
        agg, key=lambda k: (k[0], names.get(k[1], ""), k[2])
    ):
        h = agg[(date, uid, cat)]
        if h <= 0:
            continue
        writer.writerow([
            date, names.get(uid, uid), cat, round(h, 2), _hhmm(h),
            round(day_member[(date, uid)], 2),
        ])
    return buf.getvalue()


def category_totals_csv(conn: sqlite3.Connection, month: str) -> str:
    """区分別集計(月次): メンバー × 作業区分 の合計時間マトリクス (末尾に合計列)."""
    names = {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM users ORDER BY name")
    }
    agg, seen = _seg_hours_by_date_user_cat(conn, month)
    cats = _ordered_categories(conn, seen)
    # メンバーごとに区分別合計へ畳み込む
    per: dict[int, dict[str, float]] = {}
    for (date, uid, cat), h in agg.items():
        d = per.setdefault(uid, {})
        d[cat] = d.get(cat, 0.0) + h
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["メンバー"] + cats + ["合計"])
    for uid, name in sorted(names.items(), key=lambda kv: kv[1]):
        row = per.get(uid)
        if not row:
            continue
        # 各セルを先に丸め、合計は「表示セルの和」にして表と合計を一致させる
        rounded = {c: round(row[c], 2) for c in cats if c in row}
        vals = [rounded.get(c, "") for c in cats]
        writer.writerow([name] + vals + [round(sum(rounded.values()), 2)])
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
