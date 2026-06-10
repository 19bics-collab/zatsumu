"""サーバの管理画面で設定された撮影設定の取得.

撮影間隔・画質・ぼかし・撮影ON/OFF は管理画面の「設定」タブで一元管理され、
クライアントは毎サイクルこの値を取得して反映する。サーバが古い場合や
通信に失敗した場合はローカルの既定値 (CLI 引数) で動き続ける。
"""
import httpx


def fetch(client: httpx.Client, fallback: dict) -> dict:
    try:
        r = client.get("/api/me/settings")
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError:
        pass
    return {**fallback, "capture_enabled": True}
