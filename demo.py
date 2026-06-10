#!/usr/bin/env python3
"""ローカルでデモサイトを一発起動するスクリプト.

    python demo.py          (Windows は demo.bat をダブルクリックでも可)

- 必要なライブラリが無ければ自動でインストール
- デモデータ(固定トークン)を投入してサーバを起動
- ブラウザで管理画面を自動で開く
- 終了は Ctrl+C (またはウィンドウを閉じる)
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8000"))
URL = f"http://localhost:{PORT}"


def ensure_deps() -> None:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import PIL  # noqa: F401
        import multipart  # noqa: F401
    except ImportError:
        print("必要なライブラリをインストールしています (初回のみ・1分ほど)...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r",
             str(ROOT / "server" / "requirements.txt")]
        )


def open_browser_when_ready() -> None:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{URL}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    print()
    print("=" * 56)
    print(f"  デモサイトを起動しました: {URL}/admin")
    print("  管理者トークン: demo-admin")
    print("  メンバー打刻ページ: " + URL + "/me")
    print("  メンバートークン: demo-tanaka / demo-suzuki / demo-sato")
    print("  終了するには Ctrl+C")
    print("=" * 56)
    webbrowser.open(f"{URL}/admin")


def main() -> None:
    os.chdir(ROOT)
    ensure_deps()
    os.environ.setdefault("ZATSUMU_DEMO", "1")
    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    import uvicorn

    uvicorn.run("server.app:app", host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
