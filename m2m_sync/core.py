"""ブラウザに依存しない部分 (日付計算・送信・状態ファイル)。テストはここだけを対象にする."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ヤドツギへ送る項目 (これ以外は送らない。清掃スタッフ名などの個人情報を外に出さないため)
SEND_FIELDS = (
    "id", "reservationId", "listingId", "listingName", "cleaningDate",
    "status", "subStatus", "isDisabled", "hasCheckinOnDate", "updatedAt",
)


class StopError(Exception):
    """想定内の停止理由 (設定不足・ログイン失敗・取得失敗など)。code は終了コード."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass
class Config:
    email: str
    password: str
    ingest_url: str
    ingest_key: str
    days_back: int = 1
    days_ahead: int = 60
    run_at: tuple[str, ...] = ("06:00",)
    dry_run: bool = False
    data_dir: Path = Path("/data")

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"


def load_config(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    def need(k: str) -> str:
        v = (env.get(k) or "").strip()
        if not v:
            raise StopError(f"{k} が未設定 (.env を確認)", 2)
        return v

    key = need("M2M_INGEST_KEY")
    if len(key) < 32:
        raise StopError("M2M_INGEST_KEY は32文字以上 (ヤドツギ側と同じ値)", 2)
    url = need("M2M_INGEST_URL")
    if not url.startswith("https://"):
        raise StopError("M2M_INGEST_URL は https:// で始めること (合言葉を平文で流さない)", 2)
    run_at = tuple(t.strip() for t in (env.get("M2M_RUN_AT") or "06:00").split(",") if t.strip())
    for t in run_at:
        parse_hhmm(t)
    return Config(
        email=need("M2M_EMAIL"),
        password=need("M2M_PASSWORD"),
        ingest_url=url,
        ingest_key=key,
        days_back=int(env.get("M2M_DAYS_BACK") or 1),
        days_ahead=int(env.get("M2M_DAYS_AHEAD") or 60),
        run_at=run_at,
        dry_run=(env.get("M2M_DRY_RUN") or "").lower() in ("1", "true", "yes"),
        data_dir=Path(env.get("M2M_DATA_DIR") or "/data"),
    )


def jst_today(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(JST).date()


def week_windows(start: date, end: date) -> list[tuple[date, date]]:
    """[start, end] を最大7日ずつに区切る (m2m の画面と同じ1週間単位で取得する)."""
    out: list[tuple[date, date]] = []
    s = start
    while s <= end:
        e = min(s + timedelta(days=6), end)
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


def parse_hhmm(s: str) -> tuple[int, int]:
    try:
        h, m = s.split(":")
        h_i, m_i = int(h), int(m)
    except ValueError as e:
        raise StopError(f"M2M_RUN_AT の形式が違う: {s!r} (例 06:00,13:00)", 2) from e
    if not (0 <= h_i < 24 and 0 <= m_i < 60):
        raise StopError(f"M2M_RUN_AT の時刻が範囲外: {s!r}", 2)
    return h_i, m_i


def next_run(now: datetime, run_at: tuple[str, ...]) -> datetime:
    """次に実行する時刻 (JST の指定時刻のうち、now より後で最も近いもの)."""
    now_jst = now.astimezone(JST)
    cands = []
    for day in (0, 1):
        d = now_jst.date() + timedelta(days=day)
        for t in run_at:
            h, m = parse_hhmm(t)
            at = datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
            if at > now_jst:
                cands.append(at)
    return min(cands)


def build_payload(rows: list[dict], window_start: date, window_end: date) -> dict:
    """ヤドツギへ送る本文。重複を除き、送る項目を絞り、期間外の行は落とす."""
    seen: dict[str, dict] = {}
    ws, we = window_start.isoformat(), window_end.isoformat()
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid, day = r.get("id"), r.get("cleaningDate")
        if not isinstance(rid, str) or not isinstance(day, str):
            continue
        if not (ws <= day <= we):
            continue
        seen[rid] = {k: r[k] for k in SEND_FIELDS if k in r}
    return {"windowStart": ws, "windowEnd": we, "cleanings": list(seen.values())}


def post_payload(cfg: Config, payload: dict, *, retries: int = 3, sleep=time.sleep) -> dict:
    """ヤドツギの内部APIへ送る。5xx・通信エラー・409(実行中) は待って再送、その他の 4xx は即停止."""
    url = cfg.ingest_url + ("?dryRun=1" if cfg.dry_run else "")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Internal-Key": cfg.ingest_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {detail}"
            if e.code < 500 and e.code != 409:
                raise StopError(f"ヤドツギが受け付けなかった ({last})", 5) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"通信エラー: {e}"
        if attempt < retries:
            sleep(30 * attempt)
    raise StopError(f"ヤドツギへの送信に失敗 ({last})", 5)


def write_status(data_dir: Path, ok: bool, detail: dict) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        line = f"{'OK' if ok else 'FAIL'} {datetime.now(timezone.utc).isoformat()} {json.dumps(detail, ensure_ascii=False)}\n"
        (data_dir / "LAST_STATUS").write_text(line, encoding="utf-8")
    except OSError:
        pass  # 状態ファイルが書けなくても本処理の結果は変えない


def log(msg: str, **extra) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "job": "m2m-sync", "msg": msg, **extra},
                     ensure_ascii=False), flush=True)
