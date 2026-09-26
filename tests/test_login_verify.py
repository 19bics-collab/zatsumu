"""新しい端末からの管理者ログインのメール確認 (ZATSUMU_LOGIN_VERIFY_EMAIL) のテスト.

SMTP は smtplib.SMTP の差し替えで捕捉し、届いたメールから確認コードを読む。
"""
import email
import email.header
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

VERIFY_TO = "19bics@gmail.com"
DETAIL = "device_verification_required"


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
def db_path(tmp_path):
    return tmp_path / "zatsumu.db"


@pytest.fixture()
def users(client, db_path):
    from server import db
    with db.get_db(db_path) as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
        admin2 = db.create_user(conn, "boss2", is_admin=True)
        # 確認メールの送信に使う SMTP 設定 (画面から変えるには確認が要るので直接入れる)
        for k, v in {"smtp_host": "smtp.e", "smtp_port": "587", "smtp_user": "u@e",
                     "smtp_pass": "smtp-secret-pw", "mail_from": "from@e"}.items():
            db.set_setting(conn, k, v)
    return worker, admin, admin2


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("ZATSUMU_LOGIN_VERIFY_EMAIL", VERIFY_TO)


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def sendmail(self, frm, to, msg):
        if FakeSMTP.fail:
            raise OSError("smtp down smtp-secret-pw")
        FakeSMTP.sent.append((frm, to, msg))


@pytest.fixture()
def smtp(monkeypatch):
    from server import notify
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def auth(user, device=None):
    h = {"Authorization": f"Bearer {user['token']}"}
    if device:
        h["X-Device-Token"] = device
    return h


def last_mail(smtp):
    frm, to, raw = smtp.sent[-1]
    msg = email.message_from_string(raw)
    subject = str(email.header.make_header(email.header.decode_header(msg["Subject"])))
    body = msg.get_payload(decode=True).decode("utf-8")
    return to, subject, body


def code_of(body):
    return re.search(r"確認コード: (\d{6})", body).group(1)


def start(client, user):
    return client.post("/api/device/start", headers={
        **auth(user), "User-Agent": "TestBrowser/1.0 (Windows)"})


def verify_new_device(client, user, smtp):
    r = start(client, user)
    assert r.status_code == 200, r.text
    _, _, body = last_mail(smtp)
    r = client.post("/api/device/verify",
                    headers={**auth(user), "User-Agent": "TestBrowser/1.0 (Windows)"},
                    json={"request_id": r.json()["request_id"], "code": code_of(body)})
    assert r.status_code == 200, r.text
    return r.json()["device_token"]


def backdate(db_path, sql, params=()):
    from server import db
    with db.get_db(db_path) as conn:
        conn.execute(sql, params)


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


# ---------- 無効時は何も変わらない ----------

def test_disabled_keeps_existing_behavior(client, users, smtp):
    worker, admin, _ = users
    assert client.get("/api/settings", headers=auth(admin)).status_code == 200
    assert client.get("/api/status", headers=auth(admin)).status_code == 200
    assert client.get("/api/me", headers=auth(admin)).status_code == 200
    assert client.post("/api/clock-in", headers=auth(worker)).status_code == 200
    # 確認コードの API は使えない (メールも送らない)
    assert start(client, admin).status_code == 400
    assert smtp.sent == []


def test_empty_env_means_disabled(client, users, monkeypatch):
    _, admin, _ = users
    monkeypatch.setenv("ZATSUMU_LOGIN_VERIFY_EMAIL", "  ")
    assert client.get("/api/settings", headers=auth(admin)).status_code == 200


# ---------- 有効時 ----------

def test_enabled_admin_without_device_gets_401(client, users, enabled):
    _, admin, _ = users
    for path in ("/api/settings", "/api/status", "/api/mail", "/api/me", "/api/devices"):
        r = client.get(path, headers=auth(admin))
        assert r.status_code == 401 and r.json()["detail"] == DETAIL, path
    # でたらめな端末トークンも同じ
    r = client.get("/api/settings", headers=auth(admin, "bogus"))
    assert r.status_code == 401 and r.json()["detail"] == DETAIL
    # 管理者トークン自体が違うときは従来どおり (確認画面は出さない)
    r = client.get("/api/settings", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["detail"] != DETAIL


def test_enabled_members_are_not_affected(client, users, enabled):
    worker, _, _ = users
    assert client.get("/api/me", headers=auth(worker)).status_code == 200
    assert client.post("/api/clock-in", headers=auth(worker)).status_code == 200
    # 一般メンバーは管理者 API を使えない (従来どおり 403) し、確認コードも頼めない
    assert client.get("/api/settings", headers=auth(worker)).status_code == 403
    assert start(client, worker).status_code == 403


def test_start_mail_verify_and_use_device(client, users, enabled, smtp, db_path):
    _, admin, _ = users
    r = start(client, admin)
    assert r.status_code == 200
    data = r.json()
    assert data["sent_to"] == "19***@gmail.com" and data["request_id"]
    to, subject, body = last_mail(smtp)
    assert to == [VERIFY_TO]
    assert "確認コード" in subject and not re.search(r"\d{6}", subject)
    code = code_of(body)
    assert "TestBrowser/1.0" in body and "アクセス元IP" in body
    assert "誰にも教えない" in body and "作り直" in body

    r = client.post("/api/device/verify", headers=auth(admin),
                    json={"request_id": data["request_id"], "code": code})
    assert r.status_code == 200
    device = r.json()["device_token"]
    assert len(device) >= 40

    # 端末トークン付きなら使える
    assert client.get("/api/settings", headers=auth(admin, device)).status_code == 200
    assert client.get("/api/mail", headers=auth(admin, device)).status_code == 200
    # 同じコードは2回使えない
    r = client.post("/api/device/verify", headers=auth(admin),
                    json={"request_id": data["request_id"], "code": code})
    assert r.status_code == 400

    # DB にはハッシュだけ (平文のトークン・コードは保存しない)
    from server import db
    with db.get_db(db_path) as conn:
        dump = "\n".join(str(tuple(r)) for t in ("device_tokens", "device_challenges")
                         for r in conn.execute(f"SELECT * FROM {t}"))
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
        details = " ".join(r["detail"] or "" for r in conn.execute("SELECT detail FROM audit_log"))
    assert device not in dump and code not in dump.replace(admin["token"], "")
    assert "device_code_sent" in actions and "device_verify_ok" in actions
    assert device not in details and code not in details


def test_mail_body_has_no_secrets(client, users, enabled, smtp):
    _, admin, _ = users
    device = verify_new_device(client, admin, smtp)
    _, subject, body = last_mail(smtp)
    raw = smtp.sent[-1][2]
    for secret in (admin["token"], device, "smtp-secret-pw"):
        assert secret not in body and secret not in subject and secret not in raw


def test_wrong_code_five_times_invalidates(client, users, enabled, smtp, db_path):
    _, admin, _ = users
    rid = start(client, admin).json()["request_id"]
    code = code_of(last_mail(smtp)[2])
    wrong = "000000" if code != "000000" else "111111"
    for i in range(5):
        r = client.post("/api/device/verify", headers=auth(admin),
                        json={"request_id": rid, "code": wrong})
        assert r.status_code == 400
    assert "5回" in r.json()["detail"]
    # 正しいコードでも、もう通らない
    r = client.post("/api/device/verify", headers=auth(admin),
                    json={"request_id": rid, "code": code})
    assert r.status_code == 400
    from server import db
    with db.get_db(db_path) as conn:
        fails = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'device_verify_fail'"
        ).fetchone()[0]
    assert fails == 6   # 失敗は例外で巻き戻らずに記録される


def test_expired_code_is_rejected(client, users, enabled, smtp, db_path):
    _, admin, _ = users
    rid = start(client, admin).json()["request_id"]
    code = code_of(last_mail(smtp)[2])
    backdate(db_path, "UPDATE device_challenges SET expires_at = ?", (ago(seconds=1),))
    r = client.post("/api/device/verify", headers=auth(admin),
                    json={"request_id": rid, "code": code})
    assert r.status_code == 400 and "期限" in r.json()["detail"]


def test_resend_rate_limits(client, users, enabled, smtp, db_path):
    _, admin, admin2 = users
    assert start(client, admin).status_code == 200
    # 60 秒以内の再送は 429
    assert start(client, admin).status_code == 429
    # 別のユーザーは別に数える
    assert start(client, admin2).status_code == 200
    # 1時間に5回まで (60秒ずつずらして送る)
    for i in range(4):
        backdate(db_path, "UPDATE device_challenges SET created_at = ? WHERE user_id = ?"
                 " AND created_at > ?", (ago(seconds=61 + i), admin["id"], ago(seconds=61)))
        assert start(client, admin).status_code == 200
    backdate(db_path, "UPDATE device_challenges SET created_at = ? WHERE user_id = ?"
             " AND created_at > ?", (ago(seconds=70), admin["id"], ago(seconds=61)))
    r = start(client, admin)
    assert r.status_code == 429 and "1時間" in r.json()["detail"]
    assert len([m for m in smtp.sent]) == 6
    # 1時間たてばまた送れる
    backdate(db_path, "UPDATE device_challenges SET created_at = ?", (ago(hours=1, seconds=1),))
    assert start(client, admin).status_code == 200


def test_other_users_request_id_cannot_be_used(client, users, enabled, smtp):
    _, admin, admin2 = users
    rid = start(client, admin).json()["request_id"]
    code = code_of(last_mail(smtp)[2])
    r = client.post("/api/device/verify", headers=auth(admin2),
                    json={"request_id": rid, "code": code})
    assert r.status_code == 400
    # 本人はまだ使える (他人の試行で消費されない)
    r = client.post("/api/device/verify", headers=auth(admin),
                    json={"request_id": rid, "code": code})
    assert r.status_code == 200
    # 他人の端末トークンでは通らない
    device = r.json()["device_token"]
    r = client.get("/api/settings", headers=auth(admin2, device))
    assert r.status_code == 401 and r.json()["detail"] == DETAIL


def test_list_and_revoke_devices(client, users, enabled, smtp, db_path):
    _, admin, _ = users
    d1 = verify_new_device(client, admin, smtp)
    backdate(db_path, "UPDATE device_challenges SET created_at = ?", (ago(seconds=61),))
    d2 = verify_new_device(client, admin, smtp)
    r = client.get("/api/devices", headers=auth(admin, d1))
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and len(body["devices"]) == 2
    cur = [d for d in body["devices"] if d["current"]]
    assert len(cur) == 1 and cur[0]["label"].startswith("TestBrowser")
    other = next(d for d in body["devices"] if not d["current"])
    assert "token_hash" not in other

    assert client.delete(f"/api/devices/{other['id']}", headers=auth(admin, d1)).status_code == 200
    assert client.delete(f"/api/devices/{other['id']}", headers=auth(admin, d1)).status_code == 404
    r = client.get("/api/settings", headers=auth(admin, d2))
    assert r.status_code == 401 and r.json()["detail"] == DETAIL
    assert client.get("/api/settings", headers=auth(admin, d1)).status_code == 200
    from server import db
    with db.get_db(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'device_revoke'"
        ).fetchone()[0] == 1


def test_device_expires_after_90_days_unused(client, users, enabled, smtp, db_path):
    _, admin, _ = users
    device = verify_new_device(client, admin, smtp)
    backdate(db_path, "UPDATE device_tokens SET last_used_at = ?", (ago(days=89),))
    assert client.get("/api/settings", headers=auth(admin, device)).status_code == 200
    # 使ったので最終利用が今に更新され、また 90 日延びる
    from server import db
    with db.get_db(db_path) as conn:
        last = conn.execute("SELECT last_used_at FROM device_tokens").fetchone()[0]
    assert datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(minutes=1)
    backdate(db_path, "UPDATE device_tokens SET last_used_at = ?", (ago(days=90, seconds=1),))
    r = client.get("/api/settings", headers=auth(admin, device))
    assert r.status_code == 401 and r.json()["detail"] == DETAIL


def test_smtp_not_configured_returns_503(client, users, enabled, smtp, db_path):
    _, admin, _ = users
    from server import db
    with db.get_db(db_path) as conn:
        db.set_setting(conn, "smtp_host", "")
    r = start(client, admin)
    assert r.status_code == 503
    assert smtp.sent == []


def test_smtp_failure_returns_503_without_details(client, users, enabled, smtp):
    _, admin, _ = users
    smtp.fail = True
    r = start(client, admin)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "smtp-secret-pw" not in detail and "smtp down" not in detail
    assert "smtp.e" not in detail


def test_api_token_regen_revokes_devices(client, users, enabled, smtp):
    worker, admin, admin2 = users
    device2 = verify_new_device(client, admin2, smtp)
    device = verify_new_device(client, admin, smtp)
    r = client.post(f"/api/users/{admin2['id']}/token", headers=auth(admin, device))
    assert r.status_code == 200
    new = {"token": r.json()["token"]}
    r = client.get("/api/settings", headers=auth(new, device2))
    assert r.status_code == 401 and r.json()["detail"] == DETAIL
    # 端末を持つユーザーも削除できる (外部キーで止まらない)
    assert client.delete(f"/api/users/{admin2['id']}",
                         headers=auth(admin, device)).status_code == 200


def test_client_ip_trusts_forwarded_only_from_private_proxy():
    from server import devices

    class Req:
        def __init__(self, host, xff=None):
            self.client = type("C", (), {"host": host})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    # Caddy (docker 内のプライベート IP) 経由なら X-Forwarded-For の左端
    assert devices.client_ip(Req("172.18.0.3", "203.0.113.9, 10.0.0.1")) == "203.0.113.9"
    assert devices.client_ip(Req("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    # インターネットから直接来たときのヘッダは偽装できるので使わない
    assert devices.client_ip(Req("8.8.8.8", "203.0.113.9")) == "8.8.8.8"
    # 壊れたヘッダは無視
    assert devices.client_ip(Req("10.0.0.2", "<script>")) == "10.0.0.2"


def test_mask_email():
    from server import devices
    assert devices.mask_email("19bics@gmail.com") == "19***@gmail.com"
    assert devices.mask_email("ab@e.com") == "a***@e.com"


# ---------- manage.py ----------

def run_manage(monkeypatch, db_path, capsys, *args):
    import manage
    from server import db
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(sys, "argv", ["manage.py", *args])
    code = 0
    try:
        manage.main()
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def test_manage_reset_token_revokes_all_devices(client, users, enabled, smtp, db_path,
                                                monkeypatch, capsys):
    _, admin, admin2 = users
    d1 = verify_new_device(client, admin, smtp)
    d_other = verify_new_device(client, admin2, smtp)
    code, out = run_manage(monkeypatch, db_path, capsys, "reset-token", "boss")
    assert code == 0 and "1 台" in out
    new_token = re.search(r"トークン: (\S+)", out).group(1)
    r = client.get("/api/settings", headers=auth({"token": new_token}, d1))
    assert r.status_code == 401 and r.json()["detail"] == DETAIL
    # 他のユーザーの端末はそのまま
    assert client.get("/api/settings", headers=auth(admin2, d_other)).status_code == 200


def test_manage_issue_device_and_list_revoke(client, users, enabled, db_path,
                                             monkeypatch, capsys):
    worker, admin, _ = users
    code, out = run_manage(monkeypatch, db_path, capsys, "issue-device", "boss")
    assert code == 0
    device = re.search(r"#device=(\S+)", out).group(1)
    assert client.get("/api/settings", headers=auth(admin, device)).status_code == 200
    # 一般メンバーには発行しない・存在しない名前はエラー
    assert run_manage(monkeypatch, db_path, capsys, "issue-device", "tanaka")[0] == 1
    assert run_manage(monkeypatch, db_path, capsys, "issue-device", "nobody")[0] == 1

    code, out = run_manage(monkeypatch, db_path, capsys, "devices")
    assert code == 0 and "boss" in out and device not in out
    dev_id = out.split("\t")[0].strip()
    code, out = run_manage(monkeypatch, db_path, capsys, "device-revoke", dev_id)
    assert code == 0
    r = client.get("/api/settings", headers=auth(admin, device))
    assert r.status_code == 401
    code, out = run_manage(monkeypatch, db_path, capsys, "issue-device", "boss")
    code, out = run_manage(monkeypatch, db_path, capsys, "device-revoke", "all")
    assert code == 0 and "1 台" in out


# ---------- 画面 ----------

def test_templates_handle_device_verification():
    """管理画面・メール画面が確認コード画面と X-Device-Token に対応していること."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "server" / "templates"
    for name in ("mail.html", "admin.html"):
        html = (root / name).read_text(encoding="utf-8")
        assert DETAIL in html, name
        assert "X-Device-Token" in html, name
        assert "/api/device/start" in html and "/api/device/verify" in html, name
        assert 'get("device")' in html, name   # #device=<token> を受け取る


# ---------- 全角・貼り付けのコード ----------

@pytest.mark.parametrize("typed", [
    lambda c: c.translate(str.maketrans("0123456789", "０１２３４５６７８９")),  # 日本語入力のまま
    lambda c: f" {c[:3]} {c[3:]} ",                                              # 空白入りの貼り付け
    lambda c: f"{c[:3]}-{c[3:]}",
])
def test_verify_accepts_fullwidth_and_spaced_code(client, users, enabled, smtp, typed):
    """全角数字や空白入りでも、同じコードとして確認できる (日本語入力オンのまま打った場合)."""
    _, admin, _ = users
    r = start(client, admin)
    _, _, body = last_mail(smtp)
    r = client.post("/api/device/verify", headers=auth(admin),
                    json={"request_id": r.json()["request_id"], "code": typed(code_of(body))})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings",
                      headers=auth(admin, r.json()["device_token"])).status_code == 200


def test_normalize_code():
    from server.devices import normalize_code
    assert normalize_code("０７１７８４") == "071784"
    assert normalize_code("071 784") == "071784"
    assert normalize_code(None) == ""
    assert normalize_code("071785") != normalize_code("071784")


def test_templates_normalize_code_input():
    """画面側も全角を半角に直してから送る・日本語入力の確定の Enter で送らない."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "server" / "templates"
    for name in ("mail.html", "admin.html"):
        html = (root / name).read_text(encoding="utf-8")
        assert '$("dv-code").value.normalize("NFKC")' in html, name
        assert "isComposing" in html, name
        # maxlength=6 だと "123 456" の貼り付けが切れる
        tag = re.search(r'<input[^>]*id="dv-code"[^>]*>', html).group(0)
        assert 'maxlength="6"' not in tag, name
        # Caddy が /api/device を通していない (404) ときの案内がある
        assert "r.status === 404 ? DV_NOT_FOUND" in html, name


# ---------- 更新手順 (Caddy の作り直し) ----------

def test_device_start_without_token_is_401_not_404(client, users, monkeypatch):
    """DEPLOY.md の確認手順 (curl で 401 なら道が通っている) の前提.

    合言葉なしの POST /api/device/start は、機能の有効・無効にかかわらず 401。
    Caddy が古いと 404 になるので、見分けられる。
    """
    assert client.post("/api/device/start").status_code == 401
    monkeypatch.setenv("ZATSUMU_LOGIN_VERIFY_EMAIL", VERIFY_TO)
    assert client.post("/api/device/start").status_code == 401


def test_deploy_doc_recreates_caddy_on_update():
    """更新手順で Caddy を作り直す (Caddyfile は1ファイルの bind mount なので
    git pull で置き換わったファイルを、動いている Caddy は読まない)."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "DEPLOY.md").read_text(encoding="utf-8")
    update = doc.split("### 5. 更新するとき", 1)[1].split("---", 1)[0]
    assert "--force-recreate caddy" in update
    enable = doc.split("**有効にする手順**", 1)[1].split("**確認済みの端末", 1)[0]
    assert "--force-recreate caddy" in enable and "/api/device/start" in enable
