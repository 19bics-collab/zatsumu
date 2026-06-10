"""システムトレイ常駐版 zatsumu クライアント.

トレイアイコンのメニューから着席/退席をワンクリックで切り替えられる。
着席中はバックグラウンドでランダム間隔のスクリーンショットを送信する。

    python -m client.tray --server http://localhost:8000 --token <TOKEN>

注意: GUI(トレイ)環境が必要なため、ヘッドレス環境では動作しない。
依存: pystray, pillow (requirements.txt に含む)
"""
import argparse
import random
import threading

import httpx
from PIL import Image, ImageDraw

from . import capture


class Agent:
    def __init__(self, server: str, token: str, min_iv: int, max_iv: int, blur: int):
        self.client = httpx.Client(
            base_url=server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        self.min_iv, self.max_iv, self.blur = min_iv, max_iv, blur
        self.seated = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def toggle(self, icon=None, item=None):
        self.clock_out() if self.seated else self.clock_in()
        if icon:
            icon.icon = _make_icon(self.seated)
            icon.title = "zatsumu — " + ("着席中" if self.seated else "退席")

    def clock_in(self):
        r = self.client.post("/api/clock-in")
        if r.status_code not in (200, 409):
            r.raise_for_status()
        self.seated = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def clock_out(self):
        self.seated = False
        self._stop.set()
        self.client.post("/api/clock-out")

    def _capture_loop(self):
        while not self._stop.is_set():
            wait = random.randint(self.min_iv, self.max_iv)
            if self._stop.wait(wait):
                break
            try:
                jpeg = capture.to_jpeg(capture.grab_screen(), blur=self.blur)
                self.client.post(
                    "/api/screenshots",
                    files={"image": ("shot.jpg", jpeg, "image/jpeg")},
                )
            except Exception as e:  # 撮影失敗で常駐は止めない
                print(f"スクリーンショット失敗: {e}")


def _make_icon(seated: bool) -> Image.Image:
    color = (10, 125, 50) if seated else (150, 150, 150)
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


def main() -> None:
    p = argparse.ArgumentParser(description="zatsumu tray client")
    p.add_argument("--server", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--min-interval", type=int, default=180)
    p.add_argument("--max-interval", type=int, default=600)
    p.add_argument("--blur", type=int, default=0)
    args = p.parse_args()

    import pystray

    agent = Agent(
        args.server, args.token, args.min_interval, args.max_interval, args.blur
    )

    def on_quit(icon, item):
        if agent.seated:
            agent.clock_out()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(
            lambda item: "退席する" if agent.seated else "着席する", agent.toggle
        ),
        pystray.MenuItem("終了", on_quit),
    )
    icon = pystray.Icon("zatsumu", _make_icon(False), "zatsumu — 退席", menu)
    icon.run()


if __name__ == "__main__":
    main()
