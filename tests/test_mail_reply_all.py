"""CC 込みの返信 (全員に返信) のテスト.

受信メールの Cc を残し、返信画面では元の To・Cc の人 (自分たちを除く) を CC の
候補として出す。画面で直した CC・BCC は送る前に確かめ、BCC はヘッダに出さず
SMTP の送り先にだけ入れる。送信済みフォルダの控えは相手に届いたものと同じにする。
"""
import email
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

ROOT = Path(__file__).resolve().parent.parent


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


def _add_mail_with_cc(tmp_path, to_addr, cc_addr, **kw):
    """To・Cc の入った受信メールを1件入れて id を返す."""
    from server import db
    mid = _add_mail(tmp_path, **kw)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET to_addr = ?, cc_addr = ? WHERE id = ?",
                     (to_addr, cc_addr, mid))
    return mid


# 自分たち: 差出人は名前付き・大文字混じり、受信のアカウントも同じ受信箱
OWN = {"mail_from": "株式会社インビックス <Info@Bics.example>",
       "smtp_user": "smtp@bics.example", "imap_user": "INFO@bics.example"}


# ---------- 受信メールの Cc を残す ----------

def test_parse_message_keeps_cc_and_to():
    from server import mail

    # 名前は文字コード付きで届き、行を折り返し、Cc ヘッダが2つある (まとめて1つにする)
    raw = (b"From: =?utf-8?b?5bGx55Sw?= <yamada@example.com>\r\n"
           b'To: info@bics.example, "Sato, Ken" <sato@x.com>\r\n'
           b"Cc: =?utf-8?b?6Yi05pyoIOWkqumDjg==?= <suzuki@y.co.jp>,\r\n"
           b" taro..y@docomo.ne.jp\r\n"
           b"Cc: keiri@z.example\r\n"
           b"Subject: test\r\n\r\nbody\r\n")
    p = mail.parse_message(raw)
    assert p["to_addr"] == 'info@bics.example, "Sato, Ken" <sato@x.com>'
    assert p["cc_addr"] == ("鈴木 太郎 <suzuki@y.co.jp>, taro..y@docomo.ne.jp, "
                            "keiri@z.example")
    assert "\n" not in p["cc_addr"]
    assert mail.reply_all_cc(OWN, {**p, "from_addr": "yamada@example.com"}) == [
        "sato@x.com", "suzuki@y.co.jp", "taro..y@docomo.ne.jp", "keiri@z.example"]

    msg2 = EmailMessage()
    msg2["From"] = "a@example.com"
    msg2.set_content("x")
    assert mail.parse_message(msg2.as_bytes())["cc_addr"] == ""


def test_fetch_stores_cc(tmp_path, monkeypatch):
    from server import db, mail

    msg = EmailMessage()
    msg["From"] = "yamada@example.com"
    msg["To"] = "info@bics.example"
    msg["Cc"] = "staff@client.example"
    msg["Subject"] = "件名"
    msg["Message-ID"] = "<cc1@example.com>"
    msg.set_content("本文")
    raw = msg.as_bytes()

    class FakeIMAP:
        def select(self, folder, readonly=False):
            return ("OK", [b"1"])

        def response(self, key):
            return ("UIDVALIDITY", [b"1"])

        def uid(self, cmd, *args):
            if cmd == "search":
                return ("OK", [b"5"])
            return ("OK", [(b"header", raw)])

        def logout(self):
            pass

    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP())
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "mail_auto_draft": 0}
        assert len(mail.fetch_new_mail(conn, s)) == 1
        row = conn.execute("SELECT to_addr, cc_addr FROM mails").fetchone()
        assert row["to_addr"] == "info@bics.example"
        assert row["cc_addr"] == "staff@client.example"


def test_existing_db_gets_the_new_columns(tmp_path):
    """CC の列が無かった頃の DB (本番) を開くと列が足され、前のメールは空のまま使える."""
    import sqlite3

    from server import db
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
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
    old.execute("INSERT INTO mails (message_id, from_addr, to_addr, received_at,"
                " fetched_at) VALUES ('<old@e>', 'a@example.com', 'b@example.com',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    old.commit()
    old.close()

    for _ in range(2):   # 2回開いても壊れない
        conn = db.connect(path)
        row = conn.execute("SELECT * FROM mails").fetchone()
        assert row["to_addr"] == "b@example.com"
        assert (row["cc_addr"], row["reply_cc"], row["reply_bcc"]) == ("", "", "")
        conn.close()


# ---------- 全員に返信の CC 候補 ----------

def test_reply_all_cc_excludes_own_and_sender_and_dedupes():
    from server import mail

    m = {"from_addr": "Yamada@Example.com",
         "to_addr": 'info@bics.example, "Sato, Ken" <sato@x.com>',
         "cc_addr": ("SATO@x.com, yamada@example.com, staff@y.jp, smtp@BICS.example, "
                     "鈴木 <suzuki@z.example>, undisclosed-recipients:;")}
    assert mail.reply_all_cc(OWN, m) == ["sato@x.com", "staff@y.jp", "suzuki@z.example"]


def test_reply_all_cc_without_settings_or_cc():
    from server import mail

    # 自分たちのアドレスが分からなくても動く (To の人がそのまま候補になる)
    m = {"from_addr": "a@example.com", "to_addr": "info@bics.example", "cc_addr": ""}
    assert mail.reply_all_cc({}, m) == ["info@bics.example"]
    # To が自分だけ・Cc なし → 候補なし (画面ではチェックを出さない)
    assert mail.reply_all_cc(OWN, m) == []
    # ログイン名がアドレスでない設定でも落ちない
    assert mail.reply_all_cc({"smtp_user": "bics01", "imap_user": ""}, m) == [
        "info@bics.example"]


def test_reply_all_cc_broken_header_does_not_hide_the_other():
    from server import mail

    m = {"from_addr": "a@example.com", "to_addr": '"broken <x@example.com',
         "cc_addr": "b@example.com"}
    assert mail.reply_all_cc(OWN, m) == ["b@example.com"]


def test_own_addresses():
    from server import mail

    assert mail.own_addresses(OWN) == {"info@bics.example", "smtp@bics.example"}
    assert mail.own_addresses({}) == set()


# ---------- 画面から届いた CC・BCC の確認 ----------

def test_check_reply_recipients_normalizes_and_dedupes():
    from server import mail

    cc, bcc = mail.check_reply_recipients(
        "taro@example.com",
        [" sato@x.com ", "佐藤 <SATO@X.com>", "TARO@example.com", "", "taro..y@docomo.ne.jp"],
        ["boss@bics.example", "sato@x.com", "Boss@bics.example"])
    assert cc == ["sato@x.com", "taro..y@docomo.ne.jp"]   # 宛先と同じ人・重複は除く
    assert bcc == ["boss@bics.example"]                  # CC と同じ人は BCC から除く
    assert mail.check_reply_recipients("taro@example.com") == ([], [])


@pytest.mark.parametrize("bad", [
    "foo",                                   # @ が無い
    "a@b",                                   # ドメインに . が無い
    "a b@x.com",                             # 空白入り
    "a@x.com, b@y.com",                      # 1件に2つ
    "a@x.com; b@y.com",
    "<a@x.com",                              # 括弧が閉じていない
    "名前 <a@x.com> extra",
    "山田@example.jp",                       # 英数字以外
    "a@-x.com",
    "x" * 65 + "@example.com",               # @ の前が長すぎる
    "a@" + "b" * 300 + ".com",               # 全体が長すぎる
])
def test_check_reply_recipients_rejects_bad_addresses(bad):
    from server import mail

    with pytest.raises(ValueError) as ei:
        mail.check_reply_recipients("taro@example.com", [bad])
    assert "CCのアドレス" in str(ei.value)
    with pytest.raises(ValueError) as ei:
        mail.check_reply_recipients("taro@example.com", [], [bad])
    assert "BCCのアドレス" in str(ei.value)


@pytest.mark.parametrize("bad", ["a@x.com\r\nBcc: evil@x.com", "a@x.com\n", "a@x.com\x00"])
def test_check_reply_recipients_rejects_newlines(bad):
    from server import mail

    with pytest.raises(ValueError, match="改行"):
        mail.check_reply_recipients("taro@example.com", [bad])


def test_check_reply_recipients_limits_the_total():
    from server import mail

    ok = [f"u{i}@example.com" for i in range(19)]
    cc, bcc = mail.check_reply_recipients("taro@example.com", ok[:10], ok[10:])
    assert 1 + len(cc) + len(bcc) == mail.MAX_RECIPIENTS == 20
    with pytest.raises(ValueError, match="合わせて20件まで"):
        mail.check_reply_recipients("taro@example.com", ok, ["one-more@example.com"])
    with pytest.raises(ValueError, match="20件まで"):
        mail.check_reply_recipients("taro@example.com",
                                    [f"u{i}@example.com" for i in range(21)])


# ---------- 送るメール (ヘッダと SMTP の送り先) ----------

def _capture_smtp(monkeypatch):
    from server import notify

    captured = {}
    monkeypatch.setattr(
        notify, "smtp_send",
        lambda s, sender, rcpts, message: captured.update(
            sender=sender, rcpts=rcpts, msg=message))
    return captured


def test_send_reply_puts_cc_in_header_and_bcc_only_in_rcpt(monkeypatch):
    from server import mail

    captured = _capture_smtp(monkeypatch)
    raw = mail.send_reply({"smtp_host": "smtp.e", "mail_from": "info@bics.example"},
                          "taro@example.com", "Re: 見積もり", "本文です",
                          in_reply_to="<a@e.com>",
                          cc=["sato@x.com", "staff@y.jp"], bcc=["boss@bics.example"])
    assert captured["rcpts"] == ["taro@example.com", "sato@x.com", "staff@y.jp",
                                 "boss@bics.example"]
    assert raw == captured["msg"].encode("utf-8")     # 控え = 相手に届いたもの
    parsed = email.message_from_bytes(raw)
    assert parsed["To"] == "taro@example.com"
    assert parsed["Cc"] == "sato@x.com, staff@y.jp"
    assert parsed["Bcc"] is None
    assert b"boss@bics.example" not in raw            # BCC の人はどこにも出ない


def test_send_reply_without_cc_has_no_cc_header(monkeypatch):
    from server import mail

    captured = _capture_smtp(monkeypatch)
    raw = mail.send_reply({"smtp_host": "smtp.e", "mail_from": "info@bics.example"},
                          "taro@example.com", "Re: x", "本文")
    assert captured["rcpts"] == ["taro@example.com"]
    assert email.message_from_bytes(raw)["Cc"] is None


# ---------- API ----------

def _setup(client, admin, **settings):
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"smtp_host": "smtp.example.com", **settings})
    assert r.status_code == 200, r.text


def test_detail_returns_cc_candidates(client, users, tmp_path):
    _, admin = users
    _setup(client, admin, mail_from="info@bics.example", imap_user="info@bics.example")
    mid = _add_mail_with_cc(tmp_path, "Info@bics.example, sato@x.com",
                            "鈴木 <suzuki@y.jp>, taro@example.com")
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["cc_addr"] == "鈴木 <suzuki@y.jp>, taro@example.com"
    # 自分たち (info@) と、返信の宛先になる差出人 (taro@) は入らない
    assert d["reply_cc_candidates"] == ["sato@x.com", "suzuki@y.jp"]
    assert d["reply_cc"] == [] and d["reply_bcc"] == []


def test_send_with_cc_and_bcc(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    _, admin = users
    _setup(client, admin)
    mid = _add_mail_with_cc(tmp_path, "info@bics.example", "sato@x.com")
    calls = []
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: calls.append((a, kw)) or b"RAW")
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={
        "body": "ご連絡ありがとうございます。",
        "cc": ["sato@x.com", "佐藤 <Sato@X.com>", "staff@y.jp"],
        "bcc": ["boss@bics.example", "staff@y.jp"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent"] == "taro@example.com"
    assert body["cc"] == ["sato@x.com", "staff@y.jp"] and body["bcc"] == ["boss@bics.example"]
    (args, kw), = calls
    assert args[1] == "taro@example.com"
    assert kw["cc"] == ["sato@x.com", "staff@y.jp"] and kw["bcc"] == ["boss@bics.example"]
    # 実際に送った CC・BCC が記録に残る
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "replied"
    assert d["reply_cc"] == ["sato@x.com", "staff@y.jp"]
    assert d["reply_bcc"] == ["boss@bics.example"]
    # 監査ログに送り先 (件数とアドレス) が残る
    import datetime as _dt
    csv_text = client.get(
        "/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
        headers=auth(admin)).text
    assert "cc=sato@x.com,staff@y.jp" in csv_text
    assert "bcc=boss@bics.example" in csv_text
    assert "recipients=4" in csv_text


def test_send_without_cc_is_a_plain_reply(client, users, tmp_path, monkeypatch):
    """cc を渡さない (古い画面など) ときは、今までどおり差出人にだけ返す."""
    from server import app as app_module
    _, admin = users
    _setup(client, admin)
    mid = _add_mail_with_cc(tmp_path, "info@bics.example", "sato@x.com")
    calls = []
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: calls.append(kw) or b"RAW")
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 200
    assert calls[0]["cc"] == [] and calls[0]["bcc"] == []
    assert r.json()["cc"] == [] and r.json()["bcc"] == []


@pytest.mark.parametrize("payload, words", [
    ({"cc": ["not-an-address"]}, ["CCのアドレス", "not-an-address"]),
    ({"bcc": ["a@x.com\r\nX-Evil: 1"]}, ["BCC", "改行"]),
    ({"cc": [f"u{i}@example.com" for i in range(15)],
      "bcc": [f"b{i}@example.com" for i in range(6)]}, ["合わせて20件まで"]),
])
def test_send_rejects_bad_recipients(client, users, tmp_path, monkeypatch,
                                     payload, words):
    from server import app as app_module
    _, admin = users
    _setup(client, admin)
    mid = _add_mail(tmp_path)
    monkeypatch.setattr(app_module.mail, "send_reply",
                        lambda *a, **kw: pytest.fail("送ってはいけない"))
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", **payload})
    assert r.status_code == 400
    for w in words:
        assert w in r.json()["detail"]
    # 何も送らず、未対応のまま (直して送り直せる)
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "unhandled" and d["reply_cc"] == []


def test_send_failure_clears_recorded_cc(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    _, admin = users
    _setup(client, admin)
    mid = _add_mail(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module.mail, "send_reply", boom)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "cc": ["sato@x.com"], "bcc": ["b@bics.example"]})
    assert r.status_code == 502
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "unhandled"
    assert d["reply_cc"] == [] and d["reply_bcc"] == []


def test_sent_folder_copy_shows_cc_but_not_bcc(client, users, tmp_path, monkeypatch):
    """送信済みフォルダの控えは相手に届いたものと同じ (Cc はあり、Bcc は無し)."""
    from server import app as app_module
    _, admin = users
    _setup(client, admin, imap_host="imap.example.com", mail_from="info@bics.example")
    mid = _add_mail(tmp_path)
    captured = _capture_smtp(monkeypatch)
    saved = []
    monkeypatch.setattr(app_module.mail, "save_to_sent",
                        lambda s, raw: saved.append(raw) or "Sent")
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "cc": ["sato@x.com"],
                          "bcc": ["boss@bics.example"]})
    assert r.status_code == 200, r.text
    assert captured["rcpts"] == ["taro@example.com", "sato@x.com", "boss@bics.example"]
    assert saved == [captured["msg"].encode("utf-8")]
    copy = email.message_from_bytes(saved[0])
    assert copy["Cc"] == "sato@x.com" and copy["Bcc"] is None
    assert b"boss@bics.example" not in saved[0]


# ---------- 画面 ----------

def test_mail_page_has_reply_all_ui(client):
    page = client.get("/mail").text
    for marker in ('id="m-to"', 'id="m-cc-all"', 'id="m-cc"', 'id="m-bcc"',
                   "CCの人にも送る（全員に返信）",
                   "JSON.stringify({ body: text, cc, bcc })",
                   "m.reply_cc_candidates"):
        assert marker in page, marker
    # 差出人名やアドレス (外から来た文字) は textContent / value で入れる
    assert '$("m-to").textContent = ' in page
    assert 'innerHTML = `${m.' not in page
    for field in ("m-to", "m-cc", "m-bcc"):
        assert f'$("{field}").innerHTML' not in page
    # スマホでもアドレスを打ちやすく (自動の大文字化・修正をしない)
    assert page.count('inputmode="email"') >= 2
    # 送信後は CC・BCC も変えられない
    assert '$("m-cc").readOnly = $("m-bcc").readOnly = true;' in page
