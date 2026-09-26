"""この画面から送った返信の控えを、IMAP の送信済みフォルダへ保存するテスト.

SMTP で送っただけでは Webメール (お名前メール等) の「送信済み」に残らないため、
送信後に同じメールを IMAP の送信済みフォルダへ APPEND する。
"""
import email

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth


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


class FakeIMAP:
    """LIST / APPEND / SELECT だけ持つ IMAP の代役."""

    def __init__(self, folders, append_result="OK"):
        self.folders = folders
        self.append_result = append_result
        self.appended = []
        self.logged_out = False

    def list(self, directory='""', pattern="*"):
        return ("OK", self.folders)

    def select(self, mailbox, readonly=False):
        return ("OK", [b"3"])

    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, flags, date_time, message))
        return (self.append_result, [b"APPEND done"])

    def logout(self):
        self.logged_out = True


# ---------- フォルダ名 (modified UTF-7) ----------

def test_mutf7_roundtrip_japanese_folder_names():
    from server import mail

    # Outlook 等が作る「送信済みアイテム」の、サーバ上での表記
    assert mail._mutf7_decode("&kAFP4W4IMH8wojCkMMYw4A-") == "送信済みアイテム"
    assert mail._mutf7_encode("送信済みアイテム") == "&kAFP4W4IMH8wojCkMMYw4A-"
    for name in ("送信済み", "INBOX.送信済み", "A&B", "Sent Items", "受信箱/2026"):
        assert mail._mutf7_decode(mail._mutf7_encode(name)) == name
    assert mail._mutf7_encode("A&B") == "A&-B"
    assert mail._mutf7_decode("Sent") == "Sent"


def test_parse_list_handles_quotes_nil_and_literals():
    from server import mail

    data = [
        b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"',
        b'(\\HasNoChildren) "/" Drafts',
        b'(\\Noselect \\HasChildren) NIL "Public"',
        (b'(\\HasNoChildren) "/" {10}', b'My "Box"\\1'),
        b'(\\HasNoChildren) "/" "Sent \\"Old\\""',
        None,
    ]
    got = mail._parse_list(data)
    assert got[0] == ({"\\hasnochildren", "\\sent"}, ".", "INBOX.Sent")
    assert got[1] == ({"\\hasnochildren"}, "/", "Drafts")
    assert got[2] == ({"\\noselect", "\\haschildren"}, "", "Public")
    assert got[3][2] == 'My "Box"\\1'           # リテラルはそのまま
    assert got[4][2] == 'Sent "Old"'             # 引用符内のエスケープを戻す


def test_quote_mailbox_escapes():
    from server import mail

    assert mail._quote_mailbox("INBOX.Sent") == '"INBOX.Sent"'
    assert mail._quote_mailbox("Sent Items") == '"Sent Items"'
    assert mail._quote_mailbox('a"b\\c') == '"a\\"b\\\\c"'
    assert mail._quote_mailbox('"Sent"') == '"Sent"'   # 二重に囲まない


# ---------- 送信済みフォルダの見つけ方 ----------

def test_find_sent_prefers_special_use_flag():
    from server import mail

    m = FakeIMAP([
        b'(\\HasNoChildren) "." "INBOX.Sent"',
        b'(\\HasNoChildren \\Sent) "." "INBOX.&kAFP4W4IMH8wojCkMMYw4A-"',
    ])
    # 名前より「送信済み」の目印を優先する (Webメールが使うフォルダ)
    assert mail.find_sent_folder(m, {}) == "INBOX.&kAFP4W4IMH8wojCkMMYw4A-"


@pytest.mark.parametrize("line,expected", [
    (b'(\\HasNoChildren) "." "INBOX.Sent"', "INBOX.Sent"),        # Courier 系
    (b'(\\HasNoChildren) "/" Sent', "Sent"),                      # Dovecot 系
    (b'(\\HasNoChildren) "/" "Sent Messages"', "Sent Messages"),  # Apple 系
    (b'(\\HasNoChildren) "/" "&kAFP4W4IMH8wojCkMMYw4A-"',
     "&kAFP4W4IMH8wojCkMMYw4A-"),                                  # 送信済みアイテム
])
def test_find_sent_by_common_names(line, expected):
    from server import mail

    m = FakeIMAP([b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren) "/" Drafts',
                  line])
    assert mail.find_sent_folder(m, {}) == expected


def test_find_sent_skips_noselect_and_reports_when_missing():
    from server import mail

    m = FakeIMAP([b'(\\Noselect) "/" Sent', b'(\\HasNoChildren) "/" INBOX',
                  b'(\\HasNoChildren) "/" "Sentinel"'])
    with pytest.raises(RuntimeError) as ei:
        mail.find_sent_folder(m, {})
    assert "送信済みフォルダ" in str(ei.value)


def test_find_sent_uses_configured_folder():
    from server import mail

    m = FakeIMAP([b'(\\HasNoChildren \\Sent) "/" Sent'])
    # 設定があればそれを優先。英数字はそのまま、日本語はサーバの表記に直す
    assert mail.find_sent_folder(m, {"imap_sent_folder": " INBOX.Sent "}) == "INBOX.Sent"
    assert mail.find_sent_folder(m, {"imap_sent_folder": "送信済みアイテム"}) \
        == "&kAFP4W4IMH8wojCkMMYw4A-"


# ---------- 保存 (APPEND) ----------

def test_save_to_sent_appends_seen_copy(monkeypatch):
    from server import mail

    box = FakeIMAP([b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"'])
    monkeypatch.setattr(mail, "_imap_connect", lambda s: box)
    raw = b"Subject: x\r\n\r\nbody\r\n"
    assert mail.save_to_sent({"imap_host": "imap.e"}, raw) == "INBOX.Sent"
    folder, flags, date_time, message = box.appended[0]
    assert folder == '"INBOX.Sent"'
    assert flags == "(\\Seen)"                  # 既読で保存 (未読の山にしない)
    assert date_time.startswith('"') and message == raw
    assert box.logged_out


def test_save_to_sent_failure_raises_with_folder_name(monkeypatch):
    from server import mail

    box = FakeIMAP([b'(\\HasNoChildren) "/" "&kAFP4W4IMH8wojCkMMYw4A-"'],
                   append_result="NO")
    monkeypatch.setattr(mail, "_imap_connect", lambda s: box)
    with pytest.raises(RuntimeError) as ei:
        mail.save_to_sent({"imap_host": "imap.e"}, b"x")
    assert "送信済みアイテム" in str(ei.value)
    assert box.logged_out


def test_should_save_sent():
    from server import mail

    assert mail.should_save_sent({"imap_host": "imap.e", "mail_save_sent": 1})
    assert not mail.should_save_sent({"imap_host": "imap.e", "mail_save_sent": 0})
    assert not mail.should_save_sent({"imap_host": " ", "mail_save_sent": 1})


def test_imap_test_reports_sent_folder(monkeypatch):
    from server import mail

    box = FakeIMAP([b'(\\HasNoChildren) "." "INBOX.Sent"'])
    monkeypatch.setattr(mail, "_imap_connect", lambda s: box)
    assert mail.test_imap({"imap_host": "imap.e"}) == {
        "count": 3, "sent_folder": "INBOX.Sent", "sent_folder_error": ""}
    box.folders = [b'(\\HasNoChildren) "/" INBOX']
    r = mail.test_imap({"imap_host": "imap.e"})
    assert r["count"] == 3 and r["sent_folder"] == ""   # 接続自体は成功扱い
    assert "送信済みフォルダ" in r["sent_folder_error"]


# ---------- 送信するメール本体 ----------

def test_send_reply_returns_the_exact_message_sent(monkeypatch):
    """控えと相手に届くメールを同じにする (日時・Message-ID をこちらで付ける)."""
    from server import mail, notify

    captured = {}
    monkeypatch.setattr(
        notify, "smtp_send",
        lambda s, sender, rcpts, message: captured.update(msg=message))
    raw = mail.send_reply({"smtp_host": "smtp.e", "mail_from": "info@bics.example"},
                          "to@e.com", "Re: 見積もり", "本文です",
                          in_reply_to="<a@e.com>")
    assert raw == captured["msg"].encode("utf-8")
    parsed = email.message_from_bytes(raw)
    assert parsed["Date"] and parsed["Date"].endswith("+0900")   # 会社のタイムゾーン
    assert parsed["Message-ID"].endswith("@bics.example>")
    assert parsed["In-Reply-To"] == "<a@e.com>"


# ---------- 送信 API ----------

def _setup(client, admin, **settings):
    client.patch("/api/settings", headers=auth(admin),
                 json={"smtp_host": "smtp.example.com", **settings})


def test_send_saves_copy_to_sent_folder(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    _, admin = users
    mid = _add_mail(tmp_path)
    _setup(client, admin, imap_host="imap.example.com")
    monkeypatch.setattr(app_module.mail, "send_reply", lambda *a, **kw: b"RAW")
    saved = []
    monkeypatch.setattr(app_module.mail, "save_to_sent",
                        lambda s, raw: saved.append(raw) or "INBOX.Sent")
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "ありがとうございます"})
    assert r.status_code == 200
    assert r.json()["saved_to_sent"] is True
    assert r.json()["sent_folder"] == "INBOX.Sent" and saved == [b"RAW"]


def test_send_succeeds_even_if_saving_copy_fails(client, users, tmp_path,
                                                  monkeypatch):
    """控えの保存に失敗しても送信は成功扱い (再送で相手に二重に届かないように)."""
    from server import app as app_module
    _, admin = users
    mid = _add_mail(tmp_path)
    _setup(client, admin, imap_host="imap.example.com", imap_pass="imap-secret-77")
    monkeypatch.setattr(app_module.mail, "send_reply", lambda *a, **kw: b"RAW")

    def boom(s, raw):
        raise RuntimeError("APPEND failed for imap-secret-77")

    monkeypatch.setattr(app_module.mail, "save_to_sent", boom)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文"})
    assert r.status_code == 200
    body = r.json()
    assert body["saved_to_sent"] is False and "APPEND failed" in body["sent_save_error"]
    assert "imap-secret-77" not in body["sent_save_error"]   # 秘密値は伏せる
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "replied"                           # 返信済みのまま
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "x"}).status_code == 409


@pytest.mark.parametrize("settings", [
    {},                                                   # IMAP 未設定
    {"imap_host": "imap.example.com", "mail_save_sent": False},  # 設定で OFF
])
def test_send_skips_saving_when_not_applicable(client, users, tmp_path,
                                               monkeypatch, settings):
    from server import app as app_module
    _, admin = users
    mid = _add_mail(tmp_path)
    _setup(client, admin, **settings)
    monkeypatch.setattr(app_module.mail, "send_reply", lambda *a, **kw: b"RAW")

    def must_not_call(s, raw):
        raise AssertionError("保存しない設定なのに呼ばれた")

    monkeypatch.setattr(app_module.mail, "save_to_sent", must_not_call)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文"})
    assert r.status_code == 200
    assert r.json()["saved_to_sent"] is False and r.json()["sent_save_error"] == ""


def test_sent_folder_settings_roundtrip(client, users):
    _, admin = users
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_save_sent"] == 1 and s["imap_sent_folder"] == ""   # 既定: 自動
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_save_sent": False, "imap_sent_folder": "INBOX.Sent"})
    assert r.status_code == 200
    assert r.json()["mail_save_sent"] == 0 and r.json()["imap_sent_folder"] == "INBOX.Sent"
