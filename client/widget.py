"""F-Chair+ 風の常時表示ウィジェット版クライアント.

画面の隅に小さなバーを常時表示する。着席中は青(経過時間つき)、退席中は赤。
クリックで着席/退席を切り替える。右クリックで終了(着席中なら退席してから)。

    python -m client.widget --server https://kintai.example.com --token <TOKEN>

tkinter は Python 標準ライブラリのため追加インストール不要 (GUI環境は必要)。
"""
import argparse
import time
import tkinter as tk

from . import config
from .tray import Agent

SEATED_BG = "#1565c0"   # 青 = 着席中
AWAY_BG = "#c62828"     # 赤 = 退席


def main() -> None:
    p = argparse.ArgumentParser(description="zatsumu widget client")
    p.add_argument("--server")
    p.add_argument("--token")
    p.add_argument("--min-interval", type=int, default=300)
    p.add_argument("--max-interval", type=int, default=900)
    p.add_argument("--blur", type=int, default=0)
    args = p.parse_args()

    server, token = config.resolve(args.server, args.token)

    agent = Agent(
        server, token, args.min_interval, args.max_interval, args.blur
    )

    root = tk.Tk()
    root.title("zatsumu")
    root.overrideredirect(True)          # 枠なしの小さなバー
    root.attributes("-topmost", True)    # 常に最前面
    # 画面右上に配置
    root.geometry(f"170x36+{root.winfo_screenwidth() - 190}+20")

    label = tk.Label(root, font=("", 11, "bold"), fg="white")
    label.pack(fill="both", expand=True)

    def draw():
        if agent.seated and agent.since:
            mins = int(time.time() - agent.since) // 60
            label.config(
                text=f"着席中 {mins // 60}:{mins % 60:02d}", bg=SEATED_BG
            )
            root.config(bg=SEATED_BG)
        else:
            label.config(text="退席中 (クリックで着席)", bg=AWAY_BG)
            root.config(bg=AWAY_BG)

    def tick():
        draw()
        root.after(1000, tick)

    def toggle(_event=None):
        agent.clock_out() if agent.seated else agent.clock_in()
        draw()

    def quit_app(_event=None):
        if agent.seated:
            agent.clock_out()
        root.destroy()

    label.bind("<Button-1>", toggle)
    label.bind("<Button-3>", quit_app)
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
