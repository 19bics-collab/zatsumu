"""操作ウィンドウ版 zatsumu クライアント.

大きな着席/退席ボタン・状態表示・経過時間・作業区分の切り替えを持つ、
分かりやすい操作画面を表示する。スクリーンショット送信などの通信処理は
tray.Agent を再利用する。tkinter は Python 標準ライブラリ (追加導入不要)。

    python -m client.window
    python -m client.window --server https://example.com --token <TOKEN>

引数を省略すると zatsumu_config.json / 環境変数 / 初回入力から接続情報を補完する。
"""
import argparse
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox

import httpx

from . import config
from .tray import Agent

GREEN, GREEN_DK = "#16a34a", "#15803d"
RED, RED_DK = "#dc2626", "#b91c1c"
GRAY = "#64748b"
BG, CARD = "#f1f5f9", "#ffffff"
INK, SUB = "#0f172a", "#475569"


def _since_epoch(iso: str) -> float:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _popup_error(message: str) -> None:
    """まだメインウィンドウが無い段階のエラー表示."""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("勤怠管理", message)
        root.destroy()
    except Exception:
        print(message)


class Window:
    def __init__(self, root: tk.Tk, agent: Agent, me: dict, company: str):
        self.root = root
        self.agent = agent
        self.categories = me.get("categories") or []
        self.current_cat = me.get("category")
        self.hours = me.get("hours_today", 0.0)
        self.cat_btns: dict[str, tk.Button] = {}

        root.title(f"{company}")
        root.configure(bg=BG)
        root.geometry("380x470")
        root.minsize(340, 430)

        tk.Label(root, text=company, bg=BG, fg=INK,
                 font=("", 18, "bold")).pack(pady=(18, 0))
        tk.Label(root, text=f"{me.get('name', '')} さん", bg=BG, fg=SUB,
                 font=("", 12)).pack(pady=(2, 12))

        card = tk.Frame(root, bg=CARD, bd=0, highlightthickness=1,
                        highlightbackground="#e2e8f0")
        card.pack(fill="x", padx=20)
        self.status = tk.Label(card, bg=CARD, font=("", 20, "bold"))
        self.status.pack(pady=(18, 4))
        self.hours_lbl = tk.Label(card, bg=CARD, fg=SUB, font=("", 11))
        self.hours_lbl.pack(pady=(0, 16))

        self.toggle_btn = tk.Button(
            root, command=self.toggle, font=("", 17, "bold"),
            fg="white", bd=0, relief="flat", height=2, cursor="hand2",
            activeforeground="white",
        )
        self.toggle_btn.pack(fill="x", padx=20, pady=18)

        self.cat_frame = tk.Frame(root, bg=BG)
        tk.Label(self.cat_frame, text="作業区分", bg=BG, fg=SUB,
                 font=("", 10)).pack(anchor="w", padx=2)
        self.cat_btn_row = tk.Frame(self.cat_frame, bg=BG)
        self.cat_btn_row.pack(fill="x")
        for cat in self.categories:
            b = tk.Button(self.cat_btn_row, text=cat, bd=0, relief="flat",
                          font=("", 11), cursor="hand2", padx=10, pady=6,
                          command=lambda c=cat: self.set_category(c))
            b.pack(side="left", padx=(0, 6), pady=4)
            self.cat_btns[cat] = b

        self.footer = tk.Label(root, bg=BG, fg=SUB, font=("", 9))
        self.footer.pack(side="bottom", pady=8)

        # サーバ上で既に着席中だった場合は状態を復元して撮影を再開
        if me.get("seated") and me.get("open_since"):
            self.agent.resume(_since_epoch(me["open_since"]))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.draw()
        self.tick()

    # --- 操作 ---
    def toggle(self):
        try:
            self.agent.clock_out() if self.agent.seated else self.agent.clock_in()
        except httpx.HTTPError:
            messagebox.showerror(
                "勤怠管理", "サーバと通信できませんでした。ネットワークを確認してください。"
            )
        self.sync()
        self.draw()

    def set_category(self, cat: str):
        if not self.agent.seated:
            return
        try:
            self.agent.switch_category(cat)
            self.current_cat = cat
        except httpx.HTTPError:
            messagebox.showerror("勤怠管理", "区分の切り替えに失敗しました。")
        self.draw()

    def sync(self):
        """表示用の情報 (本日の在席時間・現在の区分・着席時刻) をサーバと同期."""
        try:
            me = self.agent.client.get("/api/me", timeout=15).json()
        except Exception:
            return
        self.current_cat = me.get("category")
        self.hours = me.get("hours_today", 0.0)
        if me.get("seated") and me.get("open_since"):
            self.agent.since = _since_epoch(me["open_since"])

    # --- 表示 ---
    def draw(self):
        seated = self.agent.seated
        if seated and self.agent.since:
            el = max(0, int(time.time() - self.agent.since))
            clock = f"{el // 3600}:{(el % 3600) // 60:02d}:{el % 60:02d}"
            self.status.config(text=f"🟢 着席中  {clock}", fg=GREEN_DK)
        else:
            self.status.config(text="⚪ 退席中", fg=GRAY)
        self.hours_lbl.config(text=f"本日の在席: {self.hours:.1f} 時間")
        self.toggle_btn.config(
            text="退席する" if seated else "着席する",
            bg=RED if seated else GREEN,
            activebackground=RED_DK if seated else GREEN_DK,
        )
        if seated and self.categories:
            self.cat_frame.pack(fill="x", padx=20, pady=(0, 6))
            for cat, b in self.cat_btns.items():
                on = cat == self.current_cat
                b.config(bg=GREEN if on else "#e2e8f0",
                         fg="white" if on else "#334155",
                         activebackground=GREEN_DK if on else "#cbd5e1")
        else:
            self.cat_frame.pack_forget()
        self.footer.config(text="サーバに接続中 ✓")

    def tick(self):
        if self.agent.seated:
            self.draw()
        self.root.after(1000, self.tick)

    def on_close(self):
        if self.agent.seated:
            if not messagebox.askyesno(
                "勤怠管理", "着席中です。退席して終了しますか？"
            ):
                return
            try:
                self.agent.clock_out()
            except httpx.HTTPError:
                pass
        self.root.destroy()


def main() -> None:
    p = argparse.ArgumentParser(description="zatsumu 操作ウィンドウ")
    p.add_argument("--server")
    p.add_argument("--token")
    p.add_argument("--min-interval", type=int, default=300)
    p.add_argument("--max-interval", type=int, default=900)
    p.add_argument("--blur", type=int, default=0)
    args = p.parse_args()

    server, token = config.resolve(args.server, args.token)
    agent = Agent(server, token, args.min_interval, args.max_interval, args.blur)

    # 接続確認 + 自分の情報を取得
    try:
        r = agent.client.get("/api/me")
        if r.status_code == 401:
            _popup_error("トークンが正しくありません。設定を確認してください。")
            return
        r.raise_for_status()
        me = r.json()
    except httpx.HTTPError:
        _popup_error(
            f"サーバに接続できません。ネットワークを確認してください。\n接続先: {server}"
        )
        return

    company = "勤怠管理"
    try:
        company = agent.client.get("/api/config").json().get("company_name") or company
    except httpx.HTTPError:
        pass

    root = tk.Tk()
    Window(root, agent, me, company)
    root.mainloop()


if __name__ == "__main__":
    main()
