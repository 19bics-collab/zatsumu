"""zatsumu client agent — 着席(clock-in)してランダム間隔でスクショを送信する常駐プロセス.

使い方:
    python -m client.agent --server http://localhost:8000 --token <TOKEN>

Ctrl+C で退席(clock-out)して終了する。
"""
import argparse
import random
import signal
import sys
import time

import httpx

from . import capture


def main() -> None:
    p = argparse.ArgumentParser(description="zatsumu agent")
    p.add_argument("--server", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--min-interval", type=int, default=180,
                   help="スクショ最短間隔(秒) デフォルト180")
    p.add_argument("--max-interval", type=int, default=600,
                   help="スクショ最長間隔(秒) デフォルト600")
    p.add_argument("--blur", type=int, default=0,
                   help="ぼかし強度(0=なし)。プライバシー配慮用")
    args = p.parse_args()

    client = httpx.Client(
        base_url=args.server,
        headers={"Authorization": f"Bearer {args.token}"},
        timeout=30,
    )

    r = client.post("/api/clock-in")
    if r.status_code == 409:
        print("既に着席中です。そのまま継続します。")
    else:
        r.raise_for_status()
        print("着席しました。Ctrl+C で退席します。")

    def clock_out(*_):
        print("\n退席します...")
        client.post("/api/clock-out")
        sys.exit(0)

    signal.signal(signal.SIGINT, clock_out)
    signal.signal(signal.SIGTERM, clock_out)

    while True:
        wait = random.randint(args.min_interval, args.max_interval)
        print(f"次のスクリーンショットまで {wait} 秒")
        time.sleep(wait)
        try:
            jpeg = capture.to_jpeg(capture.grab_screen(), blur=args.blur)
            client.post("/api/screenshots",
                        files={"image": ("shot.jpg", jpeg, "image/jpeg")})
            print("スクリーンショットを送信しました")
        except Exception as e:  # 撮影失敗してもエージェントは止めない
            print(f"スクリーンショット失敗: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
