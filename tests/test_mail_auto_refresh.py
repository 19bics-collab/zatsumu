"""受信箱の自動更新 (開いたままでも新着が出る) のテスト.

- 自動受信の間隔 (設定 mail_fetch_interval_min) と、定期受信のループ
- 受信の重なり防止 (定期受信・「今すぐ受信」・別のタブ)
- 受信の様子 (GET /api/mail/status: 未対応の数・一番新しいメール・最終受信・失敗の理由)
- 1分ごとの問い合わせで DB に書き込まないこと (端末の使った時刻の更新は間を空ける)

ネットワークには出ない (IMAP の受信は偽物に差し替える)。
"""
import asyncio
import imaplib
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

BUSY = "受信中です。少し待ってからもう一度押してください"


@pytest.fixture(autouse=True)
def reset_tz():
    from server import tz
    tz.set_tz("Asia/Tokyo")
    yield
    tz.set_tz("Asia/Tokyo")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZATSUMU_LOGIN_VERIFY_EMAIL", raising=False)
    import importlib
    from server import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture()
def users(client, tmp_path):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
    return worker, admin


@pytest.fixture()
def imap_on(client, users):
    """受信サーバを設定済みにする (実際の受信は各テストで差し替える)."""
    _, admin = users
    r = client.patch("/api/settings", headers=auth(admin), json={
        "imap_host": "imap.example.com", "imap_user": "info@example.com",
        "imap_pass": "imap-secret-pw", "mail_auto_draft": False})
    assert r.status_code == 200
    return admin


@pytest.fixture()
def lock_held():
    """ほかの受信の途中にする (テストの最後に必ず手放す)."""
    from server import mail
    assert mail._fetch_lock.acquire(timeout=1)
    held = {"on": True}

    def release():
        if held["on"]:
            held["on"] = False
            mail._fetch_lock.release()

    yield release
    release()


def status(client, admin):
    r = client.get("/api/mail/status", headers=auth(admin))
    assert r.status_code == 200, r.text
    return r.json()


def recent(iso, seconds=60):
    return datetime.fromisoformat(iso) > datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ---------- 自動受信の間隔 (設定) ----------

def test_fetch_interval_setting_default_and_range(client, users):
    _, admin = users
    assert client.get("/api/settings", headers=auth(admin)).json()[
        "mail_fetch_interval_min"] == 5
    for bad in (0, 61, -1):
        r = client.patch("/api/settings", headers=auth(admin),
                         json={"mail_fetch_interval_min": bad})
        assert r.status_code == 400
        # 画面に出る文は、設定の名前ではなく分かる言葉
        assert "自動受信の間隔（分） は 1〜60 の範囲" in r.json()["detail"]
    for good in (1, 60, 10):
        r = client.patch("/api/settings", headers=auth(admin),
                         json={"mail_fetch_interval_min": good})
        assert r.status_code == 200 and r.json()["mail_fetch_interval_min"] == good
    assert status(client, admin)["interval_min"] == 10


def test_mail_interval_reads_setting_each_time_and_clamps(client, tmp_path, monkeypatch):
    from server import app as m, db
    assert m._mail_interval_sec() == 5 * 60       # 既定は5分
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_fetch_interval_min", 3)
    assert m._mail_interval_sec() == 3 * 60       # 再起動しなくても次から使う
    # 手で DB を書き換えたなどで範囲外でも、1〜60分に収める
    for stored, expected in ((0, 60), (-5, 60), (999, 3600)):
        with db.get_db(tmp_path / "zatsumu.db") as conn:
            db.set_setting(conn, "mail_fetch_interval_min", stored)
        assert m._mail_interval_sec() == expected

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(m.db, "connect", boom)
    assert m._mail_interval_sec() == 5 * 60       # 読めなければ既定の値


def test_first_mail_fetch_is_soon_after_start():
    """更新・再起動のたびに、受信の間隔いっぱい新着が止まらないこと."""
    from server import app as m
    assert 0 < m.MAIL_FIRST_FETCH_DELAY_SEC <= 60
    assert m.MAIL_LOOP_RECHECK_SEC <= 60


def test_mail_loop_fetches_soon_and_follows_interval_changes(client, monkeypatch):
    """起動後すぐ1回受信し、その後は設定の間隔で受信する。間隔を変えたら、長い間隔で
    待っている途中でもすぐ使う。1回の失敗でループは止まらず、cancel では止まる."""
    from server import app as m
    ticks = []
    interval = {"sec": 1000.0}
    monkeypatch.setattr(m, "MAIL_FIRST_FETCH_DELAY_SEC", 0.01)
    monkeypatch.setattr(m, "MAIL_LOOP_RECHECK_SEC", 0.01)
    monkeypatch.setattr(m, "_mail_interval_sec", lambda: interval["sec"])

    def tick():
        ticks.append(time.monotonic())
        if len(ticks) == 1:
            raise RuntimeError("IMAP down")

    monkeypatch.setattr(m, "_mail_tick", tick)

    async def run():
        task = asyncio.create_task(m._mail_loop())
        await asyncio.sleep(0.3)
        assert len(ticks) == 1            # 起動後すぐの1回だけ (間隔の1000秒はまだ)
        assert not task.done()            # 失敗してもループは生きている
        interval["sec"] = 0.05            # 画面で間隔を短くした (再起動はしない)
        await asyncio.sleep(0.6)
        assert len(ticks) >= 3
        gaps = [b - a for a, b in zip(ticks[1:], ticks[2:])]
        assert all(g >= 0.04 for g in gaps)   # 間隔より詰めては受信しない
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.done()

    asyncio.run(run())


# ---------- 受信の重なり防止 ----------

def test_fetch_now_waits_for_running_fetch(client, imap_on, lock_held, monkeypatch):
    """ほかの受信が少しで終わるなら、終わるのを待ってから受信する (押し直さなくてよい)."""
    from server import app as m
    monkeypatch.setattr(m, "FETCH_WAIT_SEC", 5)
    released = {}
    called = []

    def fake_fetch(conn, s, **kw):
        called.append(time.monotonic())
        return [{"priority": 2}]

    monkeypatch.setattr(m.mail, "fetch_new_mail", fake_fetch)

    def finish_other():
        time.sleep(0.3)
        released["at"] = time.monotonic()
        lock_held()

    threading.Thread(target=finish_other).start()
    r = client.post("/api/mail/fetch", headers=auth(imap_on))
    assert r.status_code == 200 and r.json() == {"fetched": 1, "errors": []}
    assert called and called[0] >= released["at"]   # ほかの受信が終わってから受信した


def test_fetch_now_answers_busy_clearly(client, imap_on, lock_held, monkeypatch):
    """ほかの受信がなかなか終わらなければ、受信せずに分かりやすく伝える (409)."""
    from server import app as m
    monkeypatch.setattr(m, "FETCH_WAIT_SEC", 0.2)
    called = []
    monkeypatch.setattr(m.mail, "fetch_new_mail",
                        lambda conn, s, **kw: called.append(1) or [])
    assert status(client, imap_on)["fetching"] is True
    r = client.post("/api/mail/fetch", headers=auth(imap_on))
    assert r.status_code == 409 and r.json()["detail"] == BUSY
    assert called == []
    # 受信中だっただけなので、失敗としては記録しない
    assert status(client, imap_on)["last_error"] == ""
    lock_held()
    assert status(client, imap_on)["fetching"] is False
    r = client.post("/api/mail/fetch", headers=auth(imap_on))
    assert r.status_code == 200 and called == [1]


def test_background_fetch_skips_while_busy(client, imap_on, lock_held, tmp_path, monkeypatch):
    """「今すぐ受信」などの途中なら、定期受信はその回を飛ばす (次の回に受信する)."""
    from server import db, mail
    called = []
    monkeypatch.setattr(mail, "fetch_new_mail", lambda conn, s, **kw: called.append(1) or [])
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.check_mail(conn) == 0
    assert called == []
    lock_held()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.check_mail(conn)
    assert called == [1]


def test_fetches_never_overlap(client, imap_on, tmp_path, monkeypatch):
    """定期受信と「今すぐ受信」(2つのタブ) が同時に来ても、受信は1つずつ行う."""
    from server import db, mail
    running, peak, done = [0], [0], []
    guard = threading.Lock()

    def slow_fetch(conn, s, **kw):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.1)
        with guard:
            running[0] -= 1
        done.append(1)
        return []

    monkeypatch.setattr(mail, "fetch_new_mail", slow_fetch)

    def one():
        conn = db.connect(tmp_path / "zatsumu.db")
        try:
            mail.fetch_mail_locked(conn, db.get_settings(conn), wait=5)
        finally:
            conn.close()

    threads = [threading.Thread(target=one) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(done) == 3 and peak[0] == 1
    assert not mail.fetching()


# ---------- 受信の様子 (GET /api/mail/status) ----------

def test_status_counts_and_newest(client, users, tmp_path):
    worker, admin = users
    s = status(client, admin)
    assert s == {"unhandled": 0, "pending_approval": 0, "newest_id": None, "last_fetch_at": None,
                 "last_error": "", "last_error_at": None, "last_error_account": "",
                 "error_count": 0, "fetching": False, "accounts": [], "deleted_mail_count": 0,
                 "tabs": {"all": 0, "high": 0, "categories": {}, "uncategorized": 0,
                          "has_uncategorized": False},
                 "imap_set": False, "auto_fetch": False, "interval_min": 5}
    _add_mail(tmp_path, n=1)
    _add_mail(tmp_path, n=2, status="replied")
    newest = _add_mail(tmp_path, n=3)
    _add_mail(tmp_path, n=4, status="pending_approval")
    s = status(client, admin)
    assert s["unhandled"] == 2 and s["newest_id"] == newest + 1
    assert s["pending_approval"] == 1
    # 管理者 (とメール担当者) だけ
    assert client.get("/api/mail/status", headers=auth(worker)).status_code == 403
    assert client.get("/api/mail/status").status_code == 401
    # 自動受信の有無 (受信サーバ・自動受信の設定)
    client.patch("/api/settings", headers=auth(admin), json={"imap_host": "imap.example.com"})
    s = status(client, admin)
    assert s["imap_set"] is True and s["auto_fetch"] is True
    client.patch("/api/settings", headers=auth(admin), json={"mail_fetch_enabled": False})
    assert status(client, admin)["auto_fetch"] is False


def test_status_records_last_fetch_and_plain_error(client, imap_on, tmp_path, monkeypatch):
    from server import app as m, db, mail
    admin = imap_on
    monkeypatch.setattr(mail, "fetch_new_mail", lambda conn, s, **kw: [])
    assert client.post("/api/mail/fetch", headers=auth(admin)).status_code == 200
    s = status(client, admin)
    assert s["last_fetch_at"] and recent(s["last_fetch_at"]) and s["last_error"] == ""
    ok_at = s["last_fetch_at"]

    # 「今すぐ受信」の失敗: 分かる言葉で記録し、パスワードは出さない
    def login_failed(conn, s, **kw):
        raise imaplib.IMAP4.error(
            "b'[AUTHENTICATIONFAILED] Invalid credentials (imap-secret-pw)'")

    monkeypatch.setattr(mail, "fetch_new_mail", login_failed)
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 502
    assert "ログインできませんでした" in r.json()["detail"]
    s = status(client, admin)
    assert "ログインできませんでした" in s["last_error"]
    assert "imap-secret-pw" not in s["last_error"]
    assert s["last_error_at"] and recent(s["last_error_at"])
    assert s["last_fetch_at"] == ok_at           # 最後に受信できた時刻はそのまま

    # 定期受信の失敗も記録する (ループには例外として伝わり、ログに出る)
    def no_such_host(conn, s, **kw):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(mail, "fetch_new_mail", no_such_host)
    with pytest.raises(socket.gaierror):
        m._mail_tick()
    assert "見つかりませんでした" in status(client, admin)["last_error"]

    # 受信できたら失敗の記録は消える
    monkeypatch.setattr(mail, "fetch_new_mail", lambda conn, s, **kw: [])
    m._mail_tick()
    s = status(client, admin)
    assert s["last_error"] == "" and s["last_error_at"] is None
    assert s["last_fetch_at"] >= ok_at
    # 記録は設定の画面・API には出ない (内部の値)
    keys = client.get("/api/settings", headers=auth(admin)).json().keys()
    assert not [k for k in keys if k.startswith("mail_state_")]
    # 再起動しても残る (DB に記録している)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fetch_state(conn)["last_fetch_at"] == s["last_fetch_at"]


def test_changing_imap_settings_clears_recorded_error(client, imap_on, monkeypatch):
    from server import app as m, mail
    admin = imap_on

    def refused(conn, s, **kw):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(mail, "fetch_new_mail", refused)
    with pytest.raises(ConnectionRefusedError):
        m._mail_tick()
    assert "つながりませんでした" in status(client, admin)["last_error"]
    # 受信サーバの設定を変えずに保存しただけ (メール画面の「保存」は毎回全部送る) なら消さない
    client.patch("/api/settings", headers=auth(admin), json={
        "imap_host": "imap.example.com", "imap_use_ssl": True,
        "mail_fetch_enabled": True, "mail_signature": "署名"})
    assert status(client, admin)["last_error"]
    # 受信サーバの設定を変えたら、前の設定での失敗は消す
    client.patch("/api/settings", headers=auth(admin), json={"imap_port": "995"})
    assert status(client, admin)["last_error"] == ""


def test_status_route_is_not_taken_as_mail_id(client, users):
    """/api/mail/status は /api/mail/{mail_id} より先に宣言する (422 にならない)."""
    _, admin = users
    from server import app as m
    paths = [getattr(r, "path", "") for r in m.app.routes]
    assert paths.index("/api/mail/status") < paths.index("/api/mail/{mail_id}")
    assert client.get("/api/mail/status", headers=auth(admin)).status_code == 200


@pytest.mark.parametrize("exc, words", [
    (imaplib.IMAP4.error("LOGIN failed."), "ログインできませんでした"),
    (imaplib.IMAP4.abort("socket error: EOF"), "接続が切れました"),
    (imaplib.IMAP4.error("SEARCH command error: BAD"), "やりとりでエラー"),
    (socket.gaierror(-2, "Name or service not known"), "見つかりませんでした"),
    (ssl.SSLCertVerificationError("certificate verify failed"), "証明書"),
    (ssl.SSLError("wrong version number"), "「SSLで接続」の組み合わせ"),
    (TimeoutError("timed out"), "時間内に返事がありませんでした"),
    (socket.timeout("timed out"), "時間内に返事がありませんでした"),
    (ConnectionRefusedError(111, "Connection refused"), "IMAPポート"),
    (OSError(101, "Network is unreachable"), "つながりませんでした"),
    (RuntimeError("フォルダを開けません: INBOX"), "フォルダを開けません: INBOX"),
])
def test_fetch_error_text_is_plain(exc, words):
    from server import mail
    assert words in mail.fetch_error_text(exc)


# ---------- 1分ごとの問い合わせで DB に書き込まない ----------

def _hold_write_lock(path):
    """別の接続で書き込みのロックを持つ (この間に書き込もうとすると失敗する)."""
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    return other


def test_status_poll_does_not_write(client, users, tmp_path):
    _, admin = users
    _add_mail(tmp_path)
    other = _hold_write_lock(tmp_path / "zatsumu.db")
    try:
        started = time.monotonic()
        assert status(client, admin)["unhandled"] == 1
        assert time.monotonic() - started < 3   # 書き込みのロック待ちをしていない
    finally:
        other.rollback()
        other.close()


def test_device_touch_is_throttled_while_polling(client, users, tmp_path, monkeypatch):
    """新しい端末の確認が有効でも、1分ごとの問い合わせのたびに端末の使った時刻を書かない.

    最後に書いてから数分 (devices.TOUCH_INTERVAL) 経っていれば書く (90日で失効させるため)。
    """
    from server import db, devices
    _, admin = users
    monkeypatch.setenv("ZATSUMU_LOGIN_VERIFY_EMAIL", "19bics@gmail.com")
    assert timedelta(minutes=3) <= devices.TOUCH_INTERVAL <= timedelta(minutes=30)
    path = tmp_path / "zatsumu.db"
    with db.get_db(path) as conn:
        token = devices.issue_device(conn, admin["id"], "TestBrowser")
    headers = {**auth(admin), "X-Device-Token": token}

    def set_last_used(dt):
        with db.get_db(path) as conn:
            conn.execute("UPDATE device_tokens SET last_used_at = ?", (dt.isoformat(),))

    def last_used():
        with db.get_db(path) as conn:
            return conn.execute("SELECT last_used_at FROM device_tokens").fetchone()[0]

    # 1分前に使った端末: 書き込まない (書き込みのロックを持たれていても待たない)
    one_min_ago = datetime.now(timezone.utc) - timedelta(minutes=1)
    set_last_used(one_min_ago)
    other = _hold_write_lock(path)
    try:
        for _ in range(3):
            started = time.monotonic()
            r = client.get("/api/mail/status", headers=headers)
            assert r.status_code == 200, r.text
            assert time.monotonic() - started < 3
    finally:
        other.rollback()
        other.close()
    assert last_used() == one_min_ago.isoformat()
    # しばらく使っていなかった端末: 使った時刻を更新する
    set_last_used(datetime.now(timezone.utc) - devices.TOUCH_INTERVAL - timedelta(minutes=1))
    assert client.get("/api/mail/status", headers=headers).status_code == 200
    assert recent(last_used())


# ---------- 画面 ----------

def test_mail_page_polls_status_and_handles_visibility(client):
    page = client.get("/mail").text
    assert 'api("/api/mail/status" + scopeQuery())' in page
    assert '"visibilitychange"' in page and '"online"' in page
    assert "60 * 1000" in page                      # 1分ごと
    # どこまで見たかの記憶は、使えないブラウザでも動くよう try で囲む
    assert 'localStorage.getItem(SEEN_KEY)' in page
    assert "try {\n    const v = Number(localStorage.getItem(SEEN_KEY));" in page
    # 初めて開いたときに「見たことにした」番号もすぐ覚える (絞り込みで出ていなかった
    # 返信済みのメールに、次に開いたとき「新着」が付かないように)
    assert "saveSeen(seenId, reset);" in page
    # 自動受信の間隔の設定欄
    assert 'id="set-mail-fetch-interval"' in page
    assert "mail_fetch_interval_min: +$(\"set-mail-fetch-interval\").value" in page
