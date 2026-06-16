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
import time

import httpx
from PIL import Image, ImageDraw

from . import capture, config, settings


class Agent:
    def __init__(self, server: str, token: str, min_iv: int, max_iv: int, blur: int):
        self.client = httpx.Client(
            base_url=server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        self.min_iv, self.max_iv, self.blur = min_iv, max_iv, blur
        self.seated = False
        self.since: float | None = None  # 着席時刻 (経過時間表示用)
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
        self.since = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def clock_out(self):
        self.seated = False
        self.since = None
        self._stop.set()
        self.client.post("/api/clock-out")

    def switch_category(self, category: str):
        """在席中に作業区分を切り替える."""
        self.client.post("/api/switch-category", json={"category": category})

    def resume(self, since_epoch: float):
        """サーバ上で既に着席中だった場合に、打刻し直さず状態だけ復元して
        スクショ送信を再開する (操作ウィンドウの起動時用)."""
        self.seated = True
        self.since = since_epoch
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _capture_loop(self):
        fallback = {"min_interval": self.min_iv, "max_interval": self.max_iv,
                    "quality": 60, "blur": self.blur}
        while not self._stop.is_set():
            # 管理画面の設定を毎サイクル反映する
            conf = settings.fetch(self.client, fallback)
            if not conf["capture_enabled"]:
                if self._stop.wait(300):
                    break
                continue
            wait = random.randint(conf["min_interval"], conf["max_interval"])
            if self._stop.wait(wait):
                break
            try:
                jpeg = capture.to_jpeg(capture.grab_screen(),
                                       blur=conf["blur"], quality=conf["quality"])
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


def _show_error(message: str) -> None:
    """GUI でエラーを伝える (GUI 不可ならコンソールへ)."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("zatsumu", message)
        root.destroy()
    except Exception:
        print(message)


def main() -> None:
    p = argparse.ArgumentParser(description="zatsumu tray client")
    # 引数を省略した場合は zatsumu_config.json / 環境変数 / 初回入力から補完する
    p.add_argument("--server")
    p.add_argument("--token")
    p.add_argument("--min-interval", type=int, default=300)
    p.add_argument("--max-interval", type=int, default=900)
    p.add_argument("--blur", type=int, default=0)
    args = p.parse_args()

    server, token = config.resolve(args.server, args.token)

    import pystray

    agent = Agent(
        server, token, args.min_interval, args.max_interval, args.blur
    )

    # 起動時に接続を確認し、URL/トークン誤りを分かりやすく知らせる
    try:
        r = agent.client.get("/api/me")
        if r.status_code == 401:
            _show_error("トークンが正しくありません。設定を確認してください。")
            return
        r.raise_for_status()
    except httpx.HTTPError:
        _show_error(
            "サーバに接続できません。サーバ URL とネットワークを確認してください。\n"
            f"接続先: {server}"
        )
        return

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
