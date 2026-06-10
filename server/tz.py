"""タイムゾーン対応の時刻ヘルパー.

打刻は UTC で保存し、「今日」「当月」「日別」の境界判定はローカル
タイムゾーン (ZATSUMU_TZ、既定 Asia/Tokyo) で行う。
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("ZATSUMU_TZ", "Asia/Tokyo"))


def utc_iso(dt: datetime) -> str:
    """DB は UTC の ISO 文字列で保存しているため、SQL の文字列比較に使う
    境界値も UTC に正規化する (オフセット違いの文字列比較は時系列順にならない)."""
    return dt.astimezone(timezone.utc).isoformat()


def local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(TZ)


def today_window(now: datetime) -> tuple[datetime, datetime]:
    """ローカル日付での「今日」の [開始, 現在] を返す."""
    start = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def month_window(month: str) -> tuple[datetime, datetime]:
    """'YYYY-MM' のローカル月の [開始, 翌月開始) を返す."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=TZ)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt


def overlap_hours(
    clock_in: str, clock_out: str | None, start: datetime, end: datetime,
    now: datetime,
) -> float:
    """セッションと [start, end) の重なりを時間単位で返す (未退席は now まで)."""
    s = max(local(clock_in), start)
    e = min(local(clock_out) if clock_out else now.astimezone(TZ), end)
    return max((e - s).total_seconds() / 3600, 0.0)
