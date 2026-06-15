"""タイムゾーン対応の時刻ヘルパー.

打刻は UTC で保存し、「今日」「当月」「日別」の境界判定はローカル
タイムゾーン (ZATSUMU_TZ、既定 Asia/Tokyo) で行う。
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("ZATSUMU_TZ", "Asia/Tokyo"))


def set_tz(name: str) -> bool:
    """集計の基準タイムゾーンを切り替える。無効な名前なら False を返す."""
    global TZ
    try:
        TZ = ZoneInfo(name)
        return True
    except Exception:
        return False


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


def today_str(now: datetime | None = None) -> str:
    """ローカルタイムゾーンでの今日の日付 'YYYY-MM-DD'."""
    n = now or datetime.now(timezone.utc)
    return n.astimezone(TZ).strftime("%Y-%m-%d")


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


def day_segments(
    clock_in: str, clock_out: str | None, start: datetime, end: datetime,
    now: datetime,
):
    """セッションを [start, end) 内のローカル日付ごとの区間に分割して列挙する.

    日跨ぎのセッションは 0:00 で区切られる。未退席(clock_out=None)は now まで。
    各要素は (seg_start, seg_end, is_open) のタプル。is_open は未退席かつ末尾区間。
    在席時間集計・日別タイムライン・当日表示で共通利用する。
    """
    seg_start = max(local(clock_in), start)
    seg_close = min(local(clock_out) if clock_out else now.astimezone(TZ), end)
    while seg_start < seg_close:
        day_end = (seg_start + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seg_end = min(day_end, seg_close)
        yield seg_start, seg_end, (clock_out is None and seg_end == seg_close)
        seg_start = seg_end
