"""デモモード.

ZATSUMU_DEMO=1 で起動すると、DB が空の場合に固定トークンのデモユーザーと
架空の勤務データ・キャプチャ画像を自動投入する。本番では使わないこと
(トークンが公知のため)。

  管理者: demo-admin / メンバー: demo-tanaka, demo-suzuki, demo-sato
"""
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import tz

DEMO_USERS = [
    ("管理者", "demo-admin", 1),
    ("田中", "demo-tanaka", 0),
    ("鈴木", "demo-suzuki", 0),
    ("佐藤", "demo-sato", 0),
]


def _fake_desktop(path: Path, accent: str) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1280, 720), "#1e1e2e")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1280, 28), fill="#11111b")
    d.rectangle((40, 60, 820, 660), fill="#181825")
    for i in range(10):
        d.rectangle(
            (60, 90 + i * 50, 60 + random.randint(220, 620), 110 + i * 50),
            fill=accent,
        )
    d.rectangle((860, 60, 1240, 400), fill="#313244")
    d.rectangle((0, 692, 1280, 720), fill="#11111b")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=65)


def seed(conn: sqlite3.Connection, screenshot_dir: Path) -> bool:
    """DB が空なら投入して True を返す."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return False

    ids = {}
    for name, token, is_admin in DEMO_USERS:
        cur = conn.execute(
            "INSERT INTO users (name, token, is_admin) VALUES (?, ?, ?)",
            (name, token, is_admin),
        )
        ids[name] = cur.lastrowid

    now_utc = datetime.now(timezone.utc)
    now = now_utc.astimezone(tz.TZ)
    accents = {"田中": "#45475a", "鈴木": "#3a5a40", "佐藤": "#5a3a50"}

    def add_session(name, start, end, category="事務作業"):
        conn.execute(
            "INSERT INTO sessions (user_id, clock_in, clock_out, category) "
            "VALUES (?, ?, ?, ?)",
            (
                ids[name],
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat() if end else None,
                category,
            ),
        )

    def add_shots(name, start, end):
        t = start
        while True:
            t += timedelta(minutes=random.randint(6, 14))
            if t >= end:
                break
            rel = f"{ids[name]}/{t.strftime('%Y%m%d_%H%M%S')}.jpg"
            _fake_desktop(screenshot_dir / rel, accents[name])
            conn.execute(
                "INSERT INTO screenshots (user_id, taken_at, path) VALUES (?, ?, ?)",
                (ids[name], t.astimezone(timezone.utc).isoformat(), rel),
            )

    # 田中: 過去5日間 午前=事務作業 / 午後=現場
    for back in range(5, 0, -1):
        day = (now - timedelta(days=back)).date()
        add_session("田中", datetime.combine(day, dtime(9, 0), tz.TZ),
                    datetime.combine(day, dtime(12, 0), tz.TZ), "事務作業")
        add_session("田中", datetime.combine(day, dtime(13, 0), tz.TZ),
                    datetime.combine(day, dtime(18, 0), tz.TZ), "現場")

    # 今日: 田中=2時間前から着席中(現場) / 鈴木=45分前から着席中(事務作業)
    t_start = now - timedelta(hours=2)
    add_session("田中", t_start, None, "現場")
    add_shots("田中", t_start, now)
    s_start = now - timedelta(minutes=45)
    add_session("鈴木", s_start, None, "事務作業")
    add_shots("鈴木", s_start, now)
    add_session("佐藤", now - timedelta(hours=6), now - timedelta(hours=3),
                "事務作業")
    add_shots("佐藤", now - timedelta(hours=6), now - timedelta(hours=3))

    # デモ用の日報
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    journals = [
        ("田中", today, "・午前: 請求書の処理\n・午後: A社現場で設備点検\n明日は見積書を作成予定。"),
        ("田中", yday, "月次レポートの作成と、B社向け提案資料のレビュー対応。"),
        ("鈴木", today, "問い合わせ対応(5件)と、マニュアルの更新作業を実施。"),
        ("佐藤", today, "午前のみ勤務。経費精算をまとめて処理しました。"),
    ]
    for name, date, body in journals:
        conn.execute(
            "INSERT INTO journals (user_id, date, body, updated_at) VALUES (?,?,?,?)",
            (ids[name], date, body, now_utc.isoformat()),
        )

    conn.commit()
    return True
