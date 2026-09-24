"""m2m の管理画面をヘッドレスブラウザで開き、画面が受け取った清掃一覧を読む.

こちらから m2m の API を直接叩いたり、ログイン後の認証情報を取り出したりはしない。
「清掃一覧」画面を1週間ずつ開き、画面自身が取得した一覧 (/v4/search/cleanings の応答) を受け取るだけ。
ログイン状態は data_dir/profile に保持し、切れたときだけ ID/パスワードで入り直す。
"""
from __future__ import annotations

import os
from datetime import date

from .core import Config, StopError, log, week_windows

# 検証用に差し替え可能 (本番は既定値のまま)
BASE = os.environ.get("M2M_BASE_URL", "https://manager-cleaning.m2msystems.cloud").rstrip("/")
SEARCH_PATH = "/v4/search/cleanings"


def _is_login_screen(page) -> bool:
    if "login" in page.url.lower() or "signin" in page.url.lower():
        return True
    return page.locator('input[type="password"]').count() > 0


def _screenshot(page, cfg: Config, name: str) -> None:
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        p = cfg.data_dir / f"{name}.png"
        page.screenshot(path=str(p), full_page=True)
        log("screenshot saved", path=str(p))
    except Exception:  # noqa: BLE001
        pass


def _login(page, cfg: Config) -> None:
    log("logging in")
    pw = page.locator('input[type="password"]').first
    pw.wait_for(state="visible", timeout=20_000)
    user = page.locator(
        'input[type="email"], input[name*="mail" i], input[autocomplete="username"], input[type="text"]'
    ).first
    user.fill(cfg.email)
    pw.fill(cfg.password)
    submit = page.locator('button[type="submit"], button:has-text("ログイン"), button:has-text("Login")').first
    if submit.count():
        submit.click()
    else:
        pw.press("Enter")

    for _ in range(30):  # 最大30秒: パスワード欄が消えれば成功、認証コード欄が出たら MFA
        page.wait_for_timeout(1000)
        code = page.locator(
            'input[autocomplete="one-time-code"], input[name*="code" i], input[placeholder*="コード"]'
        ).count()
        if code:
            _screenshot(page, cfg, "mfa")
            raise StopError("2段階認証(MFA)を求められたため停止。MFAなしの取り込み専用アカウントを用意してください", 4)
        if page.locator('input[type="password"]').count() == 0:
            log("login ok")
            return
    _screenshot(page, cfg, "login-failed")
    raise StopError("m2m にログインできない (ID/パスワード誤り、または画面仕様変更)", 2)


def _fetch_week(page, cfg: Config, start: date, end: date, retried: bool = False) -> list[dict]:
    from playwright.sync_api import TimeoutError as PwTimeout

    url = (f"{BASE}/cleanings?startDate={start.isoformat()}&endDate={end.isoformat()}"
           "&listingName=&filterUnassigned=false&companyNameFilterQuery=")
    resp = None
    try:
        with page.expect_response(
            lambda r: SEARCH_PATH in r.url and r.request.method == "POST", timeout=30_000
        ) as info:
            page.goto(url, wait_until="domcontentloaded")
        resp = info.value
    except PwTimeout:
        resp = None

    if resp is None or resp.status == 401:
        if not retried and (resp is not None or _is_login_screen(page)):
            _login(page, cfg)
            return _fetch_week(page, cfg, start, end, retried=True)
        _screenshot(page, cfg, f"no-data-{start.isoformat()}")
        raise StopError(f"清掃一覧のデータを受け取れなかった ({start}〜{end})", 3)
    if not resp.ok:
        raise StopError(f"清掃一覧の取得が HTTP {resp.status} ({start}〜{end})", 3)
    body = resp.json()
    if not isinstance(body, list):
        raise StopError(f"想定外の応答形式 ({start}〜{end})", 3)
    return body


def fetch_cleanings(cfg: Config, window_start: date, window_end: date) -> list[dict]:
    """期間内の清掃一覧を返す。1週でも失敗したら StopError (部分的な結果は返さない)."""
    from playwright.sync_api import sync_playwright  # テスト環境では読み込まない

    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(cfg.profile_dir),
            headless=True,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            args=["--disable-dev-shm-usage"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for s, e in week_windows(window_start, window_end):
                week = _fetch_week(page, cfg, s, e)
                rows.extend(week)
                log("fetched week", start=s.isoformat(), end=e.isoformat(), count=len(week))
        finally:
            ctx.close()
    return rows
