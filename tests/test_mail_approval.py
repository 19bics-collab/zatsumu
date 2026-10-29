"""メール担当者 (スタッフ) の下書き → 管理者が確認して送信 のテスト.

- 役割: 管理者・メール担当者・一般メンバー・トークンなし で、メール画面と設定の
  すべての API を使えるか (使えないものは 401 / 403)
- 「確認をお願いする」→ 確認待ち → 「承認して送信」/「差し戻す」の状態の移り方
- 確認のお願いのお知らせ (返信文は載せない・外から来た文字は無害にする)
- 新しい端末の確認 (ZATSUMU_LOGIN_VERIFY_EMAIL) はメール担当者にも効く (打刻は今までどおり)
- 操作の記録・メール担当者の切り替え・設定・お知らせ/週ごとのまとめ・前からある DB・画面・manage.py

ネットワークには出ない (送信・受信・お知らせは偽物に差し替える)。
"""
import re
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth
from tests.test_login_verify import FakeSMTP, code_of, last_mail

VERIFY_TO = "19bics@gmail.com"
DEVICE = "device_verification_required"
NO_ACCESS = "mail_access_required"


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
    for key in ("ZATSUMU_DOMAIN", "ZATSUMU_MAIL_DOMAIN", "ZATSUMU_CADDYFILE"):
        monkeypatch.delenv(key, raising=False)
    import importlib
    from server import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture()
def users(client, tmp_path):
    """一般メンバー・メール担当者・管理者."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        staff = db.create_user(conn, "田中")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        admin = db.create_user(conn, "黒須", is_admin=True)
    return worker, staff, admin


@pytest.fixture()
def notices(monkeypatch):
    """確認のお願いのお知らせ (notify.deliver_async) を送らずに覚える."""
    from server import notify
    sent = []
    monkeypatch.setattr(notify, "deliver_async",
                        lambda s, text, subject="zatsumu 通知": sent.append(
                            {"text": text, "subject": subject, "settings": s}))
    return sent


def _db(tmp_path):
    from server import db
    return db.get_db(tmp_path / "zatsumu.db")


def _row(tmp_path, mail_id):
    with _db(tmp_path) as conn:
        return dict(conn.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone())


def _audits(tmp_path, action):
    with _db(tmp_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def _request(client, user, mid, body="山田 様\n\nご連絡ありがとうございます。", **kw):
    return client.post(f"/api/mail/{mid}/request-approval", headers=auth(user),
                       json={"body": body, **kw})


def _approve(tmp_path, mid, body="本文"):
    """「承認して送信」の送信内容 (画面で見ていた確認のお願いの時刻を付ける)."""
    return {"body": body, "approve_request": _row(tmp_path, mid)["requested_at"]}


def _smtp_ready(client, admin):
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"smtp_host": "smtp.example.com", "mail_from": "info@bics.example"})
    assert r.status_code == 200, r.text


# ---------- 役割ごとに使える API (すべて) ----------

# (メソッド, パス, 送る中身, メール担当者が使えるか)。{mid} などは下で本物の番号にする。
# メール画面と設定の API はここに全部並べる (増やしたら、ここにも足さないとテストが落ちる)
MATRIX = [
    ("GET", "/api/mail", None, True),
    ("POST", "/api/mail/fetch", None, True),
    ("GET", "/api/mail/status", None, True),
    ("GET", "/api/mail/me", None, True),
    ("GET", "/api/mail/assignees", None, True),
    ("GET", "/api/mail/{mid}", None, True),
    ("GET", "/api/mail/{mid}/templates", None, True),
    ("GET", "/api/mail/{mid}/attachments/{att}", None, True),
    ("POST", "/api/mail/{mid}/draft", {"instructions": ""}, True),
    ("POST", "/api/mail/{mid}/events", None, True),
    ("PATCH", "/api/mail/{mid}", {"priority": 1}, True),
    ("POST", "/api/mail/{mid}/request-approval", {"body": "本文"}, True),
    ("POST", "/api/mail/{mid}/misdirect-checked", None, True),
    # 取引先は見るだけ (変えるのは管理者だけ)
    ("GET", "/api/mail/clients", None, True),
    ("GET", "/api/mail/clients/{ckey}", None, True),
    # ここから管理者だけ
    ("POST", "/api/mail/{mid}/send", {"body": "本文"}, False),
    ("POST", "/api/mail/{mid}/send-back", {"note": "直してください"}, False),
    ("POST", "/api/mail/{mid}/archive", {"from_status": "unhandled"}, False),
    ("POST", "/api/mail/{mid}/unarchive", {"restore_to": "unhandled", "archived_at": "x"}, False),
    ("POST", "/api/mail/{mid}/notion", {}, False),
    ("GET", "/api/mail/templates", None, False),
    ("POST", "/api/mail/templates", {"name": "新しい定型文", "body": "本文"}, False),
    ("PATCH", "/api/mail/templates/{tid}", {"move": "down"}, False),
    ("DELETE", "/api/mail/templates/{tid}", None, False),
    ("GET", "/api/mail/staff", None, False),
    ("PATCH", "/api/mail/staff/{staff}", {"staff": True}, False),
    ("GET", "/api/mail/accounts", None, False),
    ("POST", "/api/mail/accounts", {"from_addr": "info@bics.example",
                                    "imap_host": "imap.example.com", "imap_pass": "pw"}, False),
    ("PATCH", "/api/mail/accounts/{acc}", {"label": "黒須"}, False),
    ("POST", "/api/mail/accounts/{acc}/test", None, False),
    ("DELETE", "/api/mail/accounts/{acc}", None, False),
    ("GET", "/api/mail/report/preview", None, False),
    ("POST", "/api/mail/report/send-now", None, False),
    ("GET", "/api/settings", None, False),
    ("PATCH", "/api/settings", {"mail_require_approval": True}, False),
    ("POST", "/api/settings/test-notify", None, False),
    ("POST", "/api/settings/test-remind", None, False),
    ("POST", "/api/settings/test-email", {"to": "boss@example.com"}, False),
    ("POST", "/api/settings/test-imap", None, False),
    ("POST", "/api/settings/test-ai", None, False),
    ("POST", "/api/settings/test-notion", None, False),
    ("GET", "/api/devices", None, False),
    ("DELETE", "/api/devices/{dev}", None, False),
    ("POST", "/api/mail/clients", {"name": "新しい取引先"}, False),
    ("PATCH", "/api/mail/clients/{ckey}", {"note": "メモ"}, False),
    ("POST", "/api/mail/clients/{ckey}/rules", {"pattern": "x@other.example.jp"}, False),
    ("DELETE", "/api/mail/clients/{ckey}/rules/{rule}", None, False),
    ("DELETE", "/api/mail/clients/{ckey}", None, False),
]


def _matrix_ids(tmp_path, staff):
    from server import db, devices, mail
    mid = _add_mail(tmp_path, n=1)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_attachments(conn, tmp_path / "mail_attachments", mid, [
            {"filename": "a.txt", "content_type": "text/plain", "size": 2, "data": b"hi",
             "skip": ""}], "2026-10-22T00:00:00+00:00")
        att = conn.execute("SELECT id FROM mail_attachments").fetchone()[0]
        tid = conn.execute("SELECT MIN(id) FROM mail_templates").fetchone()[0]
        acc = conn.execute(
            "INSERT INTO mail_accounts (label, imap_host, imap_user, imap_pass, from_addr,"
            " created_at) VALUES ('info', 'imap.example.com', 'info@bics.example', 'pw',"
            " 'info@bics.example', '2026-10-22T00:00:00+00:00')").lastrowid
        devices.issue_device(conn, staff["id"], "テスト")
        dev = conn.execute("SELECT MAX(id) FROM device_tokens").fetchone()[0]
        cl = conn.execute("INSERT INTO clients (name, created_at, updated_at)"
                          " VALUES ('取引先', 'now', 'now')").lastrowid
        rule = conn.execute("INSERT INTO client_rules (client_id, pattern, created_at)"
                            " VALUES (?, '@abc.example.jp', 'now')", (cl,)).lastrowid
    return {"mid": mid, "att": att, "tid": tid, "acc": acc, "staff": staff["id"], "dev": dev,
            "ckey": f"c:{cl}", "rule": rule}


def test_every_mail_and_settings_api_is_in_the_matrix(client):
    """メール画面・設定の API を足したら、上の表にも足す (役割の確かめ漏れを防ぐ)."""
    from server import app as app_module
    routes = set()
    for r in app_module.app.routes:
        path = getattr(r, "path", "")
        if re.match(r"/api/(mail|settings|devices?)(/|$)", path):
            for m in getattr(r, "methods", ()) - {"HEAD", "OPTIONS"}:
                routes.add((m, path))
    norm = {(m, re.sub(r"\{[a-z_]+\}", "{}", p)) for m, p in routes}
    listed = {(m, re.sub(r"\{[a-z_]+\}", "{}", p)) for m, p, _, _ in MATRIX}
    listed |= {("POST", "/api/device/start"), ("POST", "/api/device/verify")}
    assert norm == listed, norm ^ listed


def test_permission_matrix(client, users, tmp_path, monkeypatch, notices):
    from server import app as app_module
    worker, staff, admin = users
    ids = _matrix_ids(tmp_path, staff)
    # ネットワークに出ない (受信サーバへの接続・返信の送信は失敗させる)
    monkeypatch.setattr(app_module.mail, "test_imap",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no network")))
    monkeypatch.setattr(app_module.mail, "_imap_connect",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no network")))
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: pytest.fail("送信してはいけない"))
    for method, path, body, staff_ok in MATRIX:
        url = path.format(**ids)
        kw = {"json": body} if body is not None else {}
        # トークンなしは 401、一般メンバーは 403 (メール画面を使えない)
        r = client.request(method, url, **kw)
        assert r.status_code == 401, (method, url, r.status_code)
        r = client.request(method, url, headers=auth(worker), **kw)
        assert r.status_code == 403, (method, url, r.status_code)
        r = client.request(method, url, headers=auth(staff), **kw)
        if staff_ok:
            assert r.status_code not in (401, 403), (method, url, r.status_code, r.text)
        else:
            assert r.status_code == 403, (method, url, r.status_code, r.text)
        r = client.request(method, url, headers=auth(admin), **kw)
        assert r.status_code not in (401, 403), (method, url, r.status_code, r.text)
    # 確認コードの API (機能が無効なので 400)。一般メンバーは頼めない
    for path in ("/api/device/start", "/api/device/verify"):
        body = {"request_id": "x", "code": "000000"}
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers=auth(worker), json=body).status_code == 403
        assert client.post(path, headers=auth(staff), json=body).status_code == 400
        assert client.post(path, headers=auth(admin), json=body).status_code == 400


def test_member_gets_fixed_detail_so_the_page_can_log_out(client, users):
    """一般メンバー (メール担当者から外された人) の 403 は決まった文字 (画面がログイン画面に戻す)."""
    worker, _, _ = users
    for path in ("/api/mail", "/api/mail/me", "/api/mail/status"):
        r = client.get(path, headers=auth(worker))
        assert r.status_code == 403 and r.json()["detail"] == NO_ACCESS


def test_mail_me_by_role(client, users):
    worker, staff, admin = users
    a = client.get("/api/mail/me", headers=auth(admin)).json()
    s = client.get("/api/mail/me", headers=auth(staff)).json()
    assert (a["role"], a["can_send"], a["name"]) == ("admin", True, "黒須")
    assert (s["role"], s["can_send"], s["name"]) == ("staff", False, "田中")
    assert s["require_approval"] is True and "その他" in s["mail_category_list"]
    # 秘密や設定の中身は入れない (表示に要るものだけ)
    assert set(s) == {"id", "name", "role", "can_send", "require_approval", "company_name",
                      "timezone", "mail_category_list"}


def test_staff_cannot_change_status_but_can_triage(client, users, tmp_path):
    _, staff, admin = users
    mid = _add_mail(tmp_path)
    r = client.patch(f"/api/mail/{mid}", headers=auth(staff), json={"status": "archived"})
    assert r.status_code == 403 and "管理者だけ" in r.json()["detail"]
    # 状態と一緒に送ったほかの変更もしない
    r = client.patch(f"/api/mail/{mid}", headers=auth(staff),
                     json={"status": "archived", "priority": 1})
    assert r.status_code == 403
    assert _row(tmp_path, mid)["priority"] == 2
    # 優先度・種類・担当・下書きは変えられる (記録には本人の名前で残る)
    r = client.patch(f"/api/mail/{mid}", headers=auth(staff),
                     json={"priority": 1, "category": "その他", "assignee_user_id": staff["id"],
                           "draft_reply": "下書き"})
    assert r.status_code == 200, r.text
    row = _row(tmp_path, mid)
    assert (row["priority"], row["category"], row["assignee_user_id"], row["draft_reply"]) == (
        1, "その他", staff["id"], "下書き")
    assert _audits(tmp_path, "mail_update")[-1]["admin_id"] == staff["id"]
    # 「確認待ち」には PATCH ではできない (管理者でも)
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin), json={"status": "pending_approval"})
    assert r.status_code == 400


# ---------- 確認をお願いする → 確認待ち ----------

def test_request_approval_makes_it_pending_and_notifies(client, users, tmp_path, notices):
    _, staff, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": "https://hooks.slack.test/x",
                       "mail_page_url": "https://desk.example.com/mail"})
    mid = _add_mail(tmp_path, subject="<!channel> 鍵が開きません", from_name="山田",
                    from_addr="yamada@example.com")
    r = _request(client, staff, mid, cc=["sato@x.com"], bcc=["boss@bics.example"],
                 note="料金のところを\n見てください")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval" and r.json()["notified"] == ["slack"]
    row = _row(tmp_path, mid)
    assert row["status"] == "pending_approval"
    assert row["draft_reply"] == "山田 様\n\nご連絡ありがとうございます。"
    assert (row["draft_cc"], row["draft_bcc"]) == ("sato@x.com", "boss@bics.example")
    assert (row["requested_by"], row["approval_note"]) == ("田中", "料金のところを\n見てください")
    assert row["requested_at"]
    # 管理者へのお知らせ (返信文そのものは載せない。外から来た文字は無害にする)
    (n,) = notices
    assert n["subject"] == "返信の確認のお願い（田中さん）"
    assert "📝 田中さんから、メールの返信の確認のお願いが届きました" in n["text"]
    assert "＜!channel＞ 鍵が開きません — 山田（yamada@example.com）" in n["text"]
    assert "<!channel>" not in n["text"]
    assert "・ひとこと: 料金のところを 見てください" in n["text"]
    assert "受信箱を開く: https://desk.example.com/mail" in n["text"]
    assert "ご連絡ありがとうございます" not in n["text"]
    # 記録 (操作した人はメール担当者)
    (a,) = _audits(tmp_path, "mail_approval_request")
    assert a["admin_id"] == staff["id"]
    assert f"mail={mid}" in a["detail"] and "cc=sato@x.com" in a["detail"]
    assert "bcc=boss@bics.example" in a["detail"] and "note=料金のところを 見てください" in a["detail"]
    # 一覧・詳細・受信の様子
    lst = client.get("/api/mail?status=pending_approval", headers=auth(admin)).json()
    assert [(m["id"], m["requested_by"]) for m in lst] == [(mid, "田中")]
    assert client.get("/api/mail?status=unhandled",
                      headers=auth(admin)).json() == []   # 未対応には出ない
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "pending_approval" and d["requested_by"] == "田中"
    assert d["draft_cc"] == ["sato@x.com"] and d["draft_bcc"] == ["boss@bics.example"]
    st = client.get("/api/mail/status", headers=auth(staff)).json()
    assert (st["pending_approval"], st["unhandled"]) == (1, 0)


def test_pending_mail_is_locked_for_staff(client, users, tmp_path, notices):
    _, staff, admin = users
    mid = _add_mail(tmp_path)
    assert _request(client, staff, mid).status_code == 200
    # もう一度頼む・下書きを変える・作り直すのは 409
    r = _request(client, staff, mid, body="別の文")
    assert r.status_code == 409 and "もう確認をお願いしています" in r.json()["detail"]
    r = client.patch(f"/api/mail/{mid}", headers=auth(staff), json={"draft_reply": "変えた"})
    assert r.status_code == 409 and "確認待ちの間" in r.json()["detail"]
    r = client.post(f"/api/mail/{mid}/draft", headers=auth(staff), json={"instructions": ""})
    assert r.status_code == 409
    assert _row(tmp_path, mid)["draft_reply"] == "山田 様\n\nご連絡ありがとうございます。"
    # 種類・担当などの仕分けはできる
    assert client.patch(f"/api/mail/{mid}", headers=auth(staff),
                        json={"category": "その他"}).status_code == 200
    # 管理者は直せる
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin), json={"draft_reply": "直した文"})
    assert r.status_code == 200
    assert _row(tmp_path, mid)["draft_reply"] == "直した文"
    assert client.post(f"/api/mail/{mid}/draft", headers=auth(admin),
                       json={"instructions": ""}).status_code == 200


def test_staff_draft_is_not_written_if_request_arrives_meanwhile(client, users, tmp_path,
                                                                monkeypatch, notices):
    """下書きを作っている (AI を待っている) 間に確認をお願いしたら、作った文で上書きしない."""
    from server import app as app_module
    _, staff, _ = users
    mid = _add_mail(tmp_path)

    def slow_generate(s, m, instructions=""):
        with _db(tmp_path) as conn:   # 待っている間に、別の画面で確認をお願いした
            conn.execute("UPDATE mails SET status = 'pending_approval', draft_reply = '頼んだ文'"
                         " WHERE id = ?", (mid,))
        return "作った文", "template"

    monkeypatch.setattr(app_module.mail, "generate_reply", slow_generate)
    r = client.post(f"/api/mail/{mid}/draft", headers=auth(staff), json={"instructions": ""})
    assert r.status_code == 409
    assert _row(tmp_path, mid)["draft_reply"] == "頼んだ文"


@pytest.mark.parametrize("payload, code, words", [
    ({"body": "  "}, 400, ["返信文を入力"]),
    ({"body": "本文", "cc": ["not-an-address"]}, 400, ["CCのアドレス"]),
    ({"body": "本文", "note": "あ" * 501}, 400, ["500文字以内"]),
])
def test_request_validation(client, users, tmp_path, notices, payload, code, words):
    _, staff, _ = users
    mid = _add_mail(tmp_path)
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff), json=payload)
    assert r.status_code == code
    for w in words:
        assert w in r.json()["detail"]
    assert _row(tmp_path, mid)["status"] == "unhandled" and notices == []


def test_request_on_replied_or_unknown_mail(client, users, tmp_path, notices):
    _, staff, _ = users
    replied = _add_mail(tmp_path, n=1, status="replied")
    assert _request(client, staff, replied).status_code == 409
    assert _request(client, staff, 9999).status_code == 404
    noaddr = _add_mail(tmp_path, n=2, from_addr="")
    assert _request(client, staff, noaddr).status_code == 400
    # 対応不要にしたメールにも返事を書ける (確認待ちになる)
    archived = _add_mail(tmp_path, n=3, status="archived")
    assert _request(client, staff, archived).status_code == 200
    assert _row(tmp_path, archived)["status"] == "pending_approval"
    assert len(notices) == 1


def test_request_without_destinations_still_records(client, users, tmp_path, monkeypatch):
    """お知らせの送り先が無くても、頼んだことは残る (notified は空。画面で「一声かけて」と出す)."""
    from server import notify
    _, staff, _ = users
    calls = []
    monkeypatch.setattr(notify, "deliver", lambda *a, **kw: calls.append(a) or [])
    mid = _add_mail(tmp_path)
    r = _request(client, staff, mid)
    assert r.status_code == 200 and r.json()["notified"] == []
    assert calls == []   # 送り先が無いので送りに行かない
    assert _row(tmp_path, mid)["status"] == "pending_approval"


def test_setting_off_means_no_requests(client, users, tmp_path, notices):
    _, staff, admin = users
    r = client.patch("/api/settings", headers=auth(admin), json={"mail_require_approval": False})
    assert r.status_code == 200 and r.json()["mail_require_approval"] == 0
    assert "mail_require_approval=False" in _audits(tmp_path, "settings_update")[-1]["detail"]
    assert client.get("/api/mail/me", headers=auth(staff)).json()["require_approval"] is False
    mid = _add_mail(tmp_path)
    r = _request(client, staff, mid)
    assert r.status_code == 400 and "使わない設定" in r.json()["detail"]
    # オフでもメール担当者は送れない
    assert client.post(f"/api/mail/{mid}/send", headers=auth(staff),
                       json={"body": "本文"}).status_code == 403
    # 下書きは残せる
    assert client.patch(f"/api/mail/{mid}", headers=auth(staff),
                        json={"draft_reply": "下書き"}).status_code == 200


# ---------- 差し戻す ----------

def test_send_back_and_request_again(client, users, tmp_path, notices):
    _, staff, admin = users
    mid = _add_mail(tmp_path)
    assert _request(client, staff, mid, cc=["sato@x.com"], note="見てください").status_code == 200
    # コメントは必ず要る
    r = client.post(f"/api/mail/{mid}/send-back", headers=auth(admin), json={"note": "  "})
    assert r.status_code == 400 and "コメント" in r.json()["detail"]
    r = client.post(f"/api/mail/{mid}/send-back", headers=auth(admin),
                    json={"note": "到着時刻も\n書いてください"})
    assert r.status_code == 200, r.text
    assert r.json()["returned_by"] == "黒須"
    row = _row(tmp_path, mid)
    assert row["status"] == "unhandled"
    assert (row["returned_by"], row["return_note"]) == ("黒須", "到着時刻も\n書いてください")
    # 返信文・CC は残す (メール担当者がそこから直せる)
    assert row["draft_reply"] and row["draft_cc"] == "sato@x.com"
    (a,) = _audits(tmp_path, "mail_approval_return")
    assert a["admin_id"] == admin["id"]
    assert "requested_by=田中" in a["detail"] and "note=到着時刻も 書いてください" in a["detail"]
    # メール担当者には、一覧に「差し戻し」、返信画面にコメントが出る
    lst = client.get("/api/mail", headers=auth(staff)).json()
    assert [(m["id"], m["returned"]) for m in lst] == [(mid, True)]
    d = client.get(f"/api/mail/{mid}", headers=auth(staff)).json()
    assert (d["return_note"], d["returned_by"]) == ("到着時刻も\n書いてください", "黒須")
    # 確認待ちでなければ差し戻せない
    r = client.post(f"/api/mail/{mid}/send-back", headers=auth(admin), json={"note": "x"})
    assert r.status_code == 409
    # 直して頼み直すと、差し戻しのコメントは消える
    assert client.patch(f"/api/mail/{mid}", headers=auth(staff),
                        json={"draft_reply": "直した文"}).status_code == 200
    assert _request(client, staff, mid, body="直した文").status_code == 200
    row = _row(tmp_path, mid)
    assert (row["status"], row["return_note"], row["returned_by"]) == ("pending_approval", "", "")
    assert row["draft_reply"] == "直した文"
    assert len(notices) == 2
    assert client.get("/api/mail?status=unhandled", headers=auth(staff)).json() == []


def test_staff_cannot_send_back(client, users, tmp_path, notices):
    _, staff, _ = users
    mid = _add_mail(tmp_path)
    _request(client, staff, mid)
    r = client.post(f"/api/mail/{mid}/send-back", headers=auth(staff), json={"note": "x"})
    assert r.status_code == 403
    assert _row(tmp_path, mid)["status"] == "pending_approval"


# ---------- 承認して送信 ----------

def test_approve_and_send_records_approver(client, users, tmp_path, monkeypatch, notices):
    from server import app as app_module
    _, staff, admin = users
    _smtp_ready(client, admin)
    mid = _add_mail(tmp_path)
    _request(client, staff, mid, cc=["sato@x.com"])
    calls = []
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: calls.append((a, kw)) or b"RAW")
    # メール担当者は送れない (確認待ちのままで、送信もしない)
    assert client.post(f"/api/mail/{mid}/send", headers=auth(staff),
                       json={"body": "本文"}).status_code == 403
    assert calls == [] and _row(tmp_path, mid)["status"] == "pending_approval"
    # 管理者が直して送る (いつもの送信) と「承認して送信」になる
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "直した文", "cc": ["sato@x.com"],
                          "approve_request": _row(tmp_path, mid)["requested_at"]})
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == "黒須"
    (args, kw), = calls
    assert args[3] == "直した文" and kw["cc"] == ["sato@x.com"]
    row = _row(tmp_path, mid)
    assert (row["status"], row["approved_by"], row["replied_by"], row["requested_by"]) == (
        "replied", "黒須", "黒須", "田中")
    detail = _audits(tmp_path, "mail_send")[-1]["detail"]
    assert "approved_draft_by=田中" in detail
    d = client.get(f"/api/mail/{mid}", headers=auth(staff)).json()
    assert d["approved_by"] == "黒須" and d["reply_body"] == "直した文"
    # ふつうの送信 (確認待ちではない) は承認ではない
    other = _add_mail(tmp_path, n=2)
    r = client.post(f"/api/mail/{other}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 200 and r.json()["approved_by"] == ""
    assert _row(tmp_path, other)["approved_by"] == ""
    assert "approved_draft_by" not in _audits(tmp_path, "mail_send")[-1]["detail"]


def test_failed_approval_send_goes_back_to_pending(client, users, tmp_path, monkeypatch, notices):
    """送れなかったら確認待ちに戻す (管理者の「確認待ち」に残り、もう一度送れる)."""
    from server import app as app_module
    _, staff, admin = users
    _smtp_ready(client, admin)
    mid = _add_mail(tmp_path)
    _request(client, staff, mid)

    def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module.mail, "send_reply", boom)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json=_approve(tmp_path, mid))
    assert r.status_code == 502
    row = _row(tmp_path, mid)
    assert (row["status"], row["approved_by"], row["replied_by"]) == ("pending_approval", "", "")
    assert row["requested_by"] == "田中"
    # 確認待ちではなかったメールは、今までどおり未対応に戻る
    other = _add_mail(tmp_path, n=2)
    assert client.post(f"/api/mail/{other}/send", headers=auth(admin),
                       json={"body": "本文"}).status_code == 502
    assert _row(tmp_path, other)["status"] == "unhandled"


def test_refused_recipient_on_approval_goes_back_to_pending(client, users, tmp_path,
                                                            monkeypatch, notices):
    from server import app as app_module
    _, staff, admin = users
    _smtp_ready(client, admin)
    mid = _add_mail(tmp_path)
    _request(client, staff, mid)

    def refused(*a, **kw):
        raise app_module.mail.SomeRecipientsRefused(b"RAW", ["taro@example.com"])

    monkeypatch.setattr(app_module.mail, "send_reply", refused)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json=_approve(tmp_path, mid))
    assert r.status_code == 502
    assert _row(tmp_path, mid)["status"] == "pending_approval"


def test_send_from_screen_opened_before_request_is_refused(client, users, tmp_path,
                                                           monkeypatch, notices):
    """管理者が開いた後にメール担当者が確認をお願いしたら、その画面からは送らない (409).

    送ると、見ていない下書きを捨てて「承認した」ことになってしまう。開き直せば承認して送れる。
    """
    from server import app as app_module
    _, staff, admin = users
    _smtp_ready(client, admin)
    mid = _add_mail(tmp_path)
    calls = []
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: calls.append((a, kw)) or b"RAW")
    seen = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["requested_at"] or ""
    assert _request(client, staff, mid, body="田中の下書き").status_code == 200
    # 開いた画面で書いていた管理者の返信 (下書きの保存・送信) は、どちらも止まる
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_reply": "管理者の文", "seen_request": seen})
    assert r.status_code == 409 and "開き直し" in r.json()["detail"]
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "管理者の文", "approve_request": None})
    assert r.status_code == 409 and "開き直し" in r.json()["detail"]
    row = _row(tmp_path, mid)
    assert calls == []
    assert (row["status"], row["draft_reply"], row["approved_by"]) == (
        "pending_approval", "田中の下書き", "")
    assert _audits(tmp_path, "mail_send") == []
    # 開き直すと、確認のお願いの時刻が分かるので、承認して送れる
    seen = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["requested_at"]
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_reply": "田中の下書き（直した）", "seen_request": seen})
    assert r.status_code == 200
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "田中の下書き（直した）", "approve_request": seen})
    assert r.status_code == 200 and r.json()["approved_by"] == "黒須"


def test_approve_from_stale_request_is_refused(client, users, tmp_path, monkeypatch, notices):
    """「承認して送信」の画面を開いた後に、差し戻された・頼み直されたら送らない (409)."""
    from server import app as app_module
    _, staff, admin = users
    _smtp_ready(client, admin)
    mid = _add_mail(tmp_path)
    calls = []
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: calls.append((a, kw)) or b"RAW")
    _request(client, staff, mid, body="1回目")
    old = _row(tmp_path, mid)["requested_at"]
    # ほかの管理者が差し戻した → 確認待ちではないので、承認できない
    client.post(f"/api/mail/{mid}/send-back", headers=auth(admin), json={"note": "直して"})
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "1回目", "approve_request": old})
    assert r.status_code == 409 and "変わりました" in r.json()["detail"]
    # 頼み直された (別のお願い) → 前のお願いの画面からは承認できない
    with _db(tmp_path) as conn:   # 時刻が同じにならないよう、前のお願いの時刻をずらしておく
        conn.execute("UPDATE mails SET requested_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                     (mid,))
    old = _row(tmp_path, mid)["requested_at"]
    assert _request(client, staff, mid, body="2回目").status_code == 200
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "1回目", "approve_request": old})
    assert r.status_code == 409
    # 確認待ちではないメールに「承認して送信」も送らない。自分の返信 (null) なら送れる
    other = _add_mail(tmp_path, n=2)
    r = client.post(f"/api/mail/{other}/send", headers=auth(admin),
                    json={"body": "本文", "approve_request": ""})
    assert r.status_code == 409
    assert calls == [] and _row(tmp_path, mid)["status"] == "pending_approval"
    r = client.post(f"/api/mail/{other}/send", headers=auth(admin),
                    json={"body": "本文", "approve_request": None})
    assert r.status_code == 200 and r.json()["approved_by"] == ""
    # 古い画面 (seen_request を送らない) の下書きの保存は今までどおり
    third = _add_mail(tmp_path, n=3)
    r = client.patch(f"/api/mail/{third}", headers=auth(admin), json={"draft_reply": "x"})
    assert r.status_code == 200
    # 更新前に開いた画面 (approve_request の項目ごと無い) も、今までどおり承認して送れる
    # (確かめると、ページを読み込み直すまで送れなくなる)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "2回目"})
    assert r.status_code == 200 and r.json()["approved_by"] == "黒須"


def test_regenerate_draft_after_request_is_refused(client, users, tmp_path, notices):
    """管理者が開いた後に確認をお願いされたら、「返信文を生成」でもその下書きを上書きしない."""
    _, staff, admin = users
    mid = _add_mail(tmp_path)
    _request(client, staff, mid, body="田中の下書き")
    r = client.post(f"/api/mail/{mid}/draft", headers=auth(admin), json={"seen_request": ""})
    assert r.status_code == 409
    assert _row(tmp_path, mid)["draft_reply"] == "田中の下書き"
    seen = _row(tmp_path, mid)["requested_at"]
    r = client.post(f"/api/mail/{mid}/draft", headers=auth(admin), json={"seen_request": seen})
    assert r.status_code == 200
    assert _row(tmp_path, mid)["draft_reply"] == r.json()["draft_reply"]


# ---------- メール担当者の切り替え ----------

def test_grant_and_revoke_staff(client, users, tmp_path):
    worker, staff, admin = users
    lst = client.get("/api/mail/staff", headers=auth(admin)).json()
    by = {u["name"]: u for u in lst}
    assert by["黒須"]["is_admin"] and by["田中"]["staff"] and not by["tanaka"]["staff"]
    assert lst[0]["name"] == "黒須"                      # 管理者が先
    assert all("token" not in u for u in lst)
    # 一般メンバーをメール担当者にすると、メール画面を使える
    assert client.get("/api/mail", headers=auth(worker)).status_code == 403
    r = client.patch(f"/api/mail/staff/{worker['id']}", headers=auth(admin), json={"staff": True})
    assert r.status_code == 200 and r.json()["staff"] is True
    assert client.get("/api/mail", headers=auth(worker)).status_code == 200
    # 外すと、次の操作から使えない (打刻は今までどおり)
    r = client.patch(f"/api/mail/staff/{worker['id']}", headers=auth(admin), json={"staff": False})
    assert r.status_code == 200 and r.json()["staff"] is False
    r = client.get("/api/mail", headers=auth(worker))
    assert r.status_code == 403 and r.json()["detail"] == NO_ACCESS
    assert client.post("/api/clock-in", headers=auth(worker)).status_code == 200
    # 同じ値なら記録しない
    client.patch(f"/api/mail/staff/{worker['id']}", headers=auth(admin), json={"staff": False})
    logs = _audits(tmp_path, "mail_staff")
    assert [x["detail"] for x in logs] == ["staff=on tanaka", "staff=off tanaka"]
    assert all(x["target_user_id"] == worker["id"] and x["admin_id"] == admin["id"] for x in logs)
    # 管理者はメール担当者にできない (外すのは受け付ける)・いない人は 404・
    # 無効にした人はメール担当者にできない
    assert client.patch(f"/api/mail/staff/{admin['id']}", headers=auth(admin),
                        json={"staff": True}).status_code == 400
    assert client.patch("/api/mail/staff/9999", headers=auth(admin),
                        json={"staff": True}).status_code == 404
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin), json={"active": False})
    assert client.patch(f"/api/mail/staff/{worker['id']}", headers=auth(admin),
                        json={"staff": True}).status_code == 400
    # 無効にした人は、メール担当者のときだけ一覧に出る (外せるように)
    names = [u["name"] for u in client.get("/api/mail/staff", headers=auth(admin)).json()]
    assert "tanaka" not in names
    client.patch(f"/api/users/{staff['id']}", headers=auth(admin), json={"active": False})
    by = {u["name"]: u for u in client.get("/api/mail/staff", headers=auth(admin)).json()}
    assert by["田中"]["active"] is False and by["田中"]["staff"] is True
    assert client.patch(f"/api/mail/staff/{staff['id']}", headers=auth(admin),
                        json={"staff": False}).status_code == 200


def test_audit_csv_labels(client, users, tmp_path, notices):
    import datetime as _dt
    _, staff, admin = users
    mid = _add_mail(tmp_path)
    _request(client, staff, mid)
    client.post(f"/api/mail/{mid}/send-back", headers=auth(admin), json={"note": "直して"})
    client.patch(f"/api/mail/staff/{staff['id']}", headers=auth(admin), json={"staff": False})
    csv_text = client.get("/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
                          headers=auth(admin)).text
    for label in ("メール返信の確認のお願い", "メール返信の差し戻し", "メール担当者の変更"):
        assert label in csv_text
    assert "田中" in csv_text


# ---------- 新しい端末の確認 (メール担当者にも) ----------

@pytest.fixture()
def smtp(monkeypatch, client, tmp_path):
    from server import db, notify
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        for k, v in {"smtp_host": "smtp.e", "smtp_port": "587", "smtp_user": "u@e",
                     "smtp_pass": "smtp-secret-pw", "mail_from": "from@e"}.items():
            db.set_setting(conn, k, v)
    return FakeSMTP


def test_device_verification_applies_to_staff(client, users, tmp_path, smtp, monkeypatch):
    worker, staff, admin = users
    monkeypatch.setenv("ZATSUMU_LOGIN_VERIFY_EMAIL", VERIFY_TO)
    # メール画面の API は、確認していない端末では使えない
    for path in ("/api/mail", "/api/mail/me", "/api/mail/status"):
        r = client.get(path, headers=auth(staff))
        assert r.status_code == 401 and r.json()["detail"] == DEVICE, path
    # 打刻 (PC アプリ・/me) は今までどおり確認なしで使える
    assert client.get("/api/me", headers=auth(staff)).status_code == 200
    assert client.post("/api/clock-in", headers=auth(staff)).status_code == 200
    # 確認コードは決まった宛先 (管理者) に届き、メール担当者のログインだと分かる
    r = client.post("/api/device/start", headers={**auth(staff), "User-Agent": "iPhone"})
    assert r.status_code == 200, r.text
    assert r.json()["staff"] is True and r.json()["sent_to"] == "19***@gmail.com"
    to, subject, body = last_mail(smtp)
    assert to == [VERIFY_TO]
    assert "メール画面" in subject and "田中" in subject and not re.search(r"\d{6}", subject)
    assert "メール担当者の 田中 さん" in body and "本人に伝えて" in body
    assert staff["token"] not in body
    r = client.post("/api/device/verify", headers=auth(staff),
                    json={"request_id": r.json()["request_id"], "code": code_of(body)})
    assert r.status_code == 200
    device = r.json()["device_token"]
    h = {**auth(staff), "X-Device-Token": device}
    assert client.get("/api/mail", headers=h).status_code == 200
    # 確認した端末でも、管理者だけの API は使えない
    assert client.get("/api/settings", headers=h).status_code == 403
    # 管理者の画面の端末一覧に出る (取り消せる)
    with _db(tmp_path) as conn:
        assert conn.execute("SELECT user_id FROM device_tokens").fetchone()[0] == staff["id"]
    # 一般メンバーは確認コードを頼めない
    assert client.post("/api/device/start", headers=auth(worker)).status_code == 403
    # 管理者のコードは今までどおり (staff=false・管理画面の文)
    r = client.post("/api/device/start", headers=auth(admin))
    assert r.status_code == 200 and r.json()["staff"] is False
    assert "管理画面" in last_mail(smtp)[1]


# ---------- お知らせ・週ごとのまとめ ----------

class Notifier:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, text, subject="zatsumu 通知"):
        self.calls.append(text)
        return ["slack"]


def test_reminder_includes_pending_mails(client, users, tmp_path):
    from datetime import datetime, timedelta, timezone

    from server import db, mail, tz
    now = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)   # 日本時間 12:00
    conn = db.connect(tmp_path / "zatsumu.db")
    db.set_setting(conn, "slack_webhook_url", "https://hooks.slack.test/x")
    old = tz.utc_iso(now - timedelta(hours=30))
    for n, status in ((1, "pending_approval"), (2, "unhandled"), (3, "replied")):
        conn.execute(
            "INSERT INTO mails (message_id, from_addr, subject, received_at, fetched_at,"
            " status, requested_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"<p{n}@e>", f"g{n}@example.com", f"件名{n}", old, old, status,
             "田中" if status == "pending_approval" else ""))
    conn.commit()
    mail._set_state(conn, mail.REMIND_SINCE_KEY, tz.utc_iso(now - timedelta(days=3)))
    mail._set_state(conn, mail.REMIND_ACTIVE_KEY, tz.utc_iso(now - timedelta(minutes=5)))
    conn.commit()
    n = Notifier()
    assert mail.remind_unanswered(conn, notifier=n, now=now) == 2
    text = n.calls[0]
    assert "件名1" in text and "件名2" in text and "件名3" not in text
    assert "担当なし／確認待ち（田中さんの下書き）" in text
    assert "「未対応」や「確認待ち」のまま" in text
    conn.close()


def test_weekly_report_counts_pending(client, users, tmp_path):
    from datetime import date, datetime, timezone

    from server import db, mail
    conn = db.connect(tmp_path / "zatsumu.db")
    _add_mail(tmp_path, n=1, status="pending_approval")
    s = db.get_settings(conn)
    now = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    _, text = mail.weekly_report(conn, s, now, date(2026, 10, 19))
    assert "■ いま確認待ちの返信: 1通（メール担当者の下書きが、管理者の確認を待っています）" in text
    conn.close()


def test_approval_message_without_note_or_url(client):
    from server import mail
    subject, text = mail.approval_request_message(
        {"mail_page_url": ""}, {"subject": "", "from_name": "", "from_addr": "a@example.com"},
        "田中\n<@U1>")
    assert subject == "返信の確認のお願い（田中 ＜@U1＞さん）"
    assert "・(件名なし) — a@example.com" in text
    assert "ひとこと" not in text and "受信箱を開く" not in text


def test_deliver_async_passes_subject(monkeypatch):
    import threading

    from server import notify
    done = threading.Event()
    got = {}

    def fake(settings, text, subject="zatsumu 通知"):
        got.update(text=text, subject=subject)
        done.set()
        return ["slack"]

    monkeypatch.setattr(notify, "deliver", fake)
    notify.deliver_async({"slack_webhook_url": "https://hooks.slack.test/x"}, "本文", "件名")
    assert done.wait(5)
    assert got == {"text": "本文", "subject": "件名"}


# ---------- 前からある DB ----------

def test_existing_db_gets_approval_columns(tmp_path, monkeypatch, notices):
    """この機能より前の DB (本番) を開くと列が足され、そのまま確認のお願いに使える."""
    from server import db
    path = tmp_path / "zatsumu.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE,"
                " is_admin INTEGER NOT NULL DEFAULT 0)")
    old.execute("""CREATE TABLE mails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL UNIQUE, imap_uid INTEGER,
        from_addr TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '',
        to_addr TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
        priority_reason TEXT NOT NULL DEFAULT '',
        priority_source TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unhandled',
        draft_reply TEXT NOT NULL DEFAULT '', draft_source TEXT NOT NULL DEFAULT '',
        draft_generated_at TEXT, reply_subject TEXT NOT NULL DEFAULT '',
        reply_body TEXT NOT NULL DEFAULT '', replied_at TEXT,
        replied_by TEXT NOT NULL DEFAULT '', references_hdr TEXT NOT NULL DEFAULT '')""")
    old.execute("INSERT INTO users (name, token, is_admin) VALUES ('黒須', 'tok-admin', 1)")
    old.execute("INSERT INTO users (name, token) VALUES ('田中', 'tok-staff')")
    old.execute("INSERT INTO mails (message_id, from_addr, subject, received_at,"
                " fetched_at) VALUES ('<old@e>', 'a@example.com', '前のメール',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    old.commit()
    old.close()
    for _ in range(2):   # 2回開いても壊れない
        conn = db.connect(path)
        user = conn.execute("SELECT * FROM users WHERE name = '田中'").fetchone()
        assert user["mail_role"] == "" and db.mail_role(user) == ""
        row = conn.execute("SELECT * FROM mails").fetchone()
        assert (row["requested_by"], row["requested_at"], row["approved_by"],
                row["return_note"], row["draft_cc"]) == ("", None, "", "", "")
        assert db.get_settings(conn)["mail_require_approval"] == 1
        assert not conn.in_transaction
        conn.close()
    import importlib
    from server import app as app_module
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZATSUMU_LOGIN_VERIFY_EMAIL", raising=False)
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    admin, staff = {"token": "tok-admin"}, {"token": "tok-staff"}
    assert c.get("/api/mail", headers=auth(staff)).status_code == 403
    assert c.patch("/api/mail/staff/2", headers=auth(admin), json={"staff": True}).status_code == 200
    assert _request(c, staff, 1).status_code == 200
    assert c.get("/api/mail/1", headers=auth(admin)).json()["status"] == "pending_approval"


# ---------- 画面 ----------

def test_mail_page_has_role_aware_ui(client):
    page = client.get("/mail").text
    for marker in ('"/api/mail/me"', 'id="m-request"', 'id="m-return"', 'id="m-appr"',
                   'id="staff-card"', 'id="set-require-approval"', '"/api/mail/staff"',
                   'value="pending_approval"', 'id="nav-pending"', 'id="approval-banner"',
                   "approve_request: approving", "seen_request: seenRequest()",
                   "request-approval", "send-back", "mail_require_approval: $(",
                   'pending_approval: "確認待ち"', 'id="dv-staff"'):
        assert marker in page, marker
    # 確認待ちの数は受信箱のボタンの中に入れない (スマホで受信箱を押すと当たってしまう)
    assert 'id="nav-inbox">受信箱<span id="inbox-badge" class="ntf" hidden></span></button>' in page
    # 設定のボタンは最初は出さない (管理者だと分かってから出す)
    assert '<button id="nav-settings" hidden>' in page
    # メール担当者には送信・対応不要・設定を出さない (サーバも 403 で止める)
    for line in ('$("m-send").hidden = !admin;', '$("m-archive").hidden = !admin;',
                 '$("nav-settings").hidden = !admin;', "if (!isAdmin()) return;"):
        assert line in page, line
    # 確認待ちの間は、メール担当者の返信文・CC・BCC を変えられない
    assert "const locked = replied || (!admin && pending);" in page
    # 名前・ひとこと・コメントは textContent / esc() で入れる
    for line in ('$("m-appr-head").textContent = head;', '$("m-appr-note").textContent = note;',
                 "<b>${esc(u.name)}</b>", "<small>（${esc(m.requested_by)}）</small>"):
        assert line in page, line
    # メール担当者から外されたら (決まった 403)、ログイン画面に戻す
    assert 'const NO_MAIL_ACCESS = "mail_access_required";' in page


# ---------- manage.py ----------

def _manage(monkeypatch, db_path, capsys, *args):
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


def test_manage_add_mail_staff_and_issue_device(client, users, tmp_path, monkeypatch, capsys):
    path = tmp_path / "zatsumu.db"
    code, out = _manage(monkeypatch, path, capsys, "add-user", "佐藤", "--mail-staff")
    assert code == 0 and "メール担当者にしました" in out
    token = re.search(r"トークン: (\S+)", out).group(1)
    r = client.get("/api/mail/me", headers=auth({"token": token}))
    assert r.status_code == 200 and r.json()["role"] == "staff"
    code, out = _manage(monkeypatch, path, capsys, "list-users")
    assert "佐藤\tmail-staff" in out and "tanaka\tuser" in out and "黒須\tadmin" in out
    # 端末トークンはメール担当者にも発行できる (一般メンバーにはできない)
    assert _manage(monkeypatch, path, capsys, "issue-device", "田中")[0] == 0
    assert _manage(monkeypatch, path, capsys, "issue-device", "tanaka")[0] == 1


def test_switching_admin_clears_mail_staff(client, users, tmp_path):
    """メール担当者を管理者にして、あとで管理者から外しても、メール担当者の権限が知らないうちに
    戻らない (勤怠の管理画面にはメール担当者の印が出ないので、気づけないため)."""
    worker, staff, admin = users
    r = client.patch(f"/api/users/{staff['id']}", headers=auth(admin), json={"is_admin": True})
    assert r.status_code == 200
    by = {u["name"]: u for u in client.get("/api/mail/staff", headers=auth(admin)).json()}
    assert by["田中"]["is_admin"] is True and by["田中"]["staff"] is False
    r = client.patch(f"/api/users/{staff['id']}", headers=auth(admin), json={"is_admin": False})
    assert r.status_code == 200
    r = client.get("/api/mail", headers=auth(staff))
    assert r.status_code == 403 and r.json()["detail"] == NO_ACCESS
    assert [x["detail"] for x in _audits(tmp_path, "mail_staff")] == [
        "staff=off 田中 (管理者の切り替え)"]
    # 管理者に印が残っていても (前に付いたもの)、メール画面の設定から外せる
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (admin["id"],))
    assert client.patch(f"/api/mail/staff/{admin['id']}", headers=auth(admin),
                        json={"staff": True}).status_code == 400
    r = client.patch(f"/api/mail/staff/{admin['id']}", headers=auth(admin), json={"staff": False})
    assert r.status_code == 200 and r.json()["staff"] is False
    # 管理者でない人の切り替えでは、印はそのまま (ほかの項目を変えても消えない)
    client.patch(f"/api/mail/staff/{worker['id']}", headers=auth(admin), json={"staff": True})
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"is_admin": False, "email": "w@example.com"})
    assert client.get("/api/mail", headers=auth(worker)).status_code == 200
