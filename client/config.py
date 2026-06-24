"""接続設定 (サーバ URL・トークン) の解決.

優先順位: CLI 引数 > 環境変数 > ユーザー設定ファイル > 配布時に同梱した既定ファイル。
トークンが無く GUI が使える場合は入力ダイアログを出し、ユーザー設定に保存する。
これにより .exe をダブルクリックするだけ (初回だけトークン入力) で運用できる。

配布の想定:
  zatsumu.exe と同じフォルダに  zatsumu_config.json  を置き、{"server": "https://..."}
  を書いておく。初回起動でメンバーが自分のトークンを入力すると、以降は
  %APPDATA%\\zatsumu\\config.json に保存され、ダブルクリックだけで着席できる。
"""
import json
import os
import sys
from pathlib import Path

ENV_SERVER = "ZATSUMU_SERVER"
ENV_TOKEN = "ZATSUMU_TOKEN"

# 単体exeを設定ファイル無しで配っても繋がるよう、既定の接続先を埋め込む。
# 最低優先度(同梱config/ユーザ設定/環境変数/CLIで上書き可)。
DEFAULT_SERVER = "https://kintai.yadotsugi.jp"


def user_config_path() -> Path:
    """ユーザーごとのトークン保存先 (Windows は %APPDATA%、他は ~/.config)."""
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "zatsumu" / "config.json"


def _bundled_config_paths() -> list[Path]:
    """配布時に同梱する既定設定の探索先 (server URL を入れておく)."""
    paths = []
    if getattr(sys, "frozen", False):  # PyInstaller の .exe
        paths.append(Path(sys.executable).resolve().parent / "zatsumu_config.json")
    paths.append(Path.cwd() / "zatsumu_config.json")
    return paths


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(server: str | None = None, token: str | None = None) -> dict:
    """全ソースをマージした設定 dict を返す (後勝ち=優先度高)."""
    cfg: dict = {"server": DEFAULT_SERVER}   # 最低優先度の既定接続先
    for p in _bundled_config_paths():
        cfg.update(_read_json(p))
    cfg.update(_read_json(user_config_path()))
    if os.environ.get(ENV_SERVER):
        cfg["server"] = os.environ[ENV_SERVER]
    if os.environ.get(ENV_TOKEN):
        cfg["token"] = os.environ[ENV_TOKEN]
    if server:
        cfg["server"] = server
    if token:
        cfg["token"] = token
    return cfg


def save_token(token: str) -> None:
    """トークンをユーザー設定ファイルに保存する (server は上書きしない)."""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json(path)
    data["token"] = token
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def prompt_token(message: str | None = None) -> str | None:
    """GUI でトークンを尋ねる。入力されたら保存して返す (GUI 不可なら None)."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        token = simpledialog.askstring(
            "勤怠管理 トークン設定",
            message or "あなたのトークンを入力してください\n(管理者から配布されたもの)",
            show="*",
        )
    finally:
        root.destroy()
    if token and token.strip():
        token = token.strip()
        save_token(token)
        return token
    return None


def resolve(
    server: str | None = None,
    token: str | None = None,
    *,
    allow_prompt: bool = True,
) -> tuple[str, str]:
    """サーバ URL とトークンを確定する。

    トークンが未設定で GUI が使えるときは入力ダイアログを出す。どうしても
    揃わなければ SystemExit で分かりやすいメッセージを出して終了する。
    """
    cfg = load(server, token)
    s = cfg.get("server")
    t = cfg.get("token")
    if not t and allow_prompt:
        t = prompt_token()
    if not s:
        raise SystemExit(
            "サーバ URL が設定されていません。zatsumu_config.json に "
            '{"server": "https://..."} を記載するか、--server で指定してください。'
        )
    if not t:
        raise SystemExit(
            "トークンが設定されていません。初回起動で入力するか、"
            "--token で指定してください。"
        )
    return s, t
