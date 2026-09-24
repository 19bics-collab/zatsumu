"""使い方:
    python -m m2m_sync --once      1回だけ実行して終了 (試運転・手動実行)
    python -m m2m_sync             常駐し、M2M_RUN_AT (JST, 既定 06:00) に毎日実行
    M2M_DRY_RUN=true を付けると、ヤドツギは DB を変えずに結果だけ返す
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from .core import StopError, build_payload, jst_today, load_config, log, next_run, post_payload, write_status


def run_once(cfg) -> int:
    from .fetch import fetch_cleanings

    today = jst_today()
    ws, we = today - timedelta(days=cfg.days_back), today + timedelta(days=cfg.days_ahead)
    log("start", windowStart=ws.isoformat(), windowEnd=we.isoformat(), dryRun=cfg.dry_run)
    try:
        rows = fetch_cleanings(cfg, ws, we)
        payload = build_payload(rows, ws, we)
        if not payload["cleanings"]:
            raise StopError("取得件数が0件のため送信しない (画面仕様変更・絞り込み条件の変化を確認)", 3)
        result = post_payload(cfg, payload)
        log("done", sent=len(payload["cleanings"]), **{k: result.get(k) for k in
            ("dryRun", "created", "linked", "refreshed", "moved", "cancelled")})
        for w in result.get("warnings") or []:
            log("要確認", detail=w)
        if result.get("unmatchedNames"):
            log("物件名がヤドツギと一致しない (ヤドツギ側の対応表に追記)", names=result["unmatchedNames"])
        write_status(cfg.data_dir, True, {
            "sent": len(payload["cleanings"]), "created": result.get("created"), "moved": result.get("moved"),
            "cancelled": result.get("cancelled"), "warnings": len(result.get("warnings") or []),
            "unmatched": len(result.get("unmatchedNames") or []), "dryRun": cfg.dry_run,
        })
        return 0
    except StopError as e:
        log("failed", error=str(e), code=e.code)
        write_status(cfg.data_dir, False, {"error": str(e)})
        return e.code
    except Exception as e:  # noqa: BLE001  想定外でも常駐は止めない
        log("failed", error=repr(e), code=1)
        write_status(cfg.data_dir, False, {"error": repr(e)})
        return 1


def main(argv: list[str]) -> int:
    try:
        cfg = load_config()
    except StopError as e:
        log("config error", error=str(e))
        return e.code

    if "--once" in argv:
        return run_once(cfg)

    log("daemon started", runAt=list(cfg.run_at))
    while True:
        at = next_run(datetime.now(timezone.utc), cfg.run_at)
        log("next run", at=at.isoformat())
        while (wait := (at - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(min(wait, 300))
        code = run_once(cfg)
        if code not in (0, 2, 4):  # 取得・送信の一時的な失敗は10分後に1回だけ再挑戦
            time.sleep(600)
            run_once(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
