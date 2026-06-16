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

from . import capture, config, settings


def main() -> None:
    p = argparse.ArgumentParser(description="zatsumu agent")
    p.add_argument("--server")
    p.add_argument("--token")
    # 既定は平均10分(1時間に約6回)のランダム間隔。F-Chair+ の標準と同等
    p.add_argument("--min-interval", type=int, default=300,
                   help="スクショ最短間隔(秒) デフォルト300")
    p.add_argument("--max-interval", type=int, default=900,
                   help="スクショ最長間隔(秒) デフォルト900")
    p.add_argument("--blur", type=int, default=0,
                   help="ぼかし強度(0=なし)。プライバシー配慮用")
    args = p.parse_args()

    # CLI で省略した接続情報は設定ファイル/環境変数から補完する
    server, token = config.resolve(args.server, args.token, allow_prompt=False)

    client = httpx.Client(
        base_url=server,
        headers={"Authorization": f"Bearer {token}"},
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

    fallback = {"min_interval": args.min_interval,
                "max_interval": args.max_interval,
                "quality": 60, "blur": args.blur}
    first = True
    while True:
        # 管理画面の設定を毎サイクル反映する (間隔・画質・ぼかし・撮影ON/OFF)
        conf = settings.fetch(client, fallback)
        if not conf["capture_enabled"]:
            print("撮影は管理者により停止中です (打刻のみ記録)")
            time.sleep(300)
            continue
        # 着席直後の1枚目は短い待ちで撮る(動作確認しやすく・記録の取りこぼし防止)
        if first:
            wait = random.randint(15, 45)
            first = False
        else:
            wait = random.randint(conf["min_interval"], conf["max_interval"])
        print(f"次のスクリーンショットまで {wait} 秒")
        time.sleep(wait)
        try:
            jpeg = capture.to_jpeg(capture.grab_screen(),
                                   blur=conf["blur"], quality=conf["quality"])
            client.post("/api/screenshots",
                        files={"image": ("shot.jpg", jpeg, "image/jpeg")})
            print("スクリーンショットを送信しました")
        except Exception as e:  # 撮影失敗してもエージェントは止めない
            print(f"スクリーンショット失敗: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
