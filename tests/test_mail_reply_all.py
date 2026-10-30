"""CC 込みの返信 (全員に返信) のテスト.

受信メールの Cc を残し、返信画面では元の To・Cc の人 (自分たちを除く) を CC の
候補として出す。画面で直した CC・BCC は送る前に確かめ、BCC はヘッダに出さず
SMTP の送り先にだけ入れる。送信済みフォルダの控えは相手に届いたものと同じにする。
"""
import email
import email.policy
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
        assert row["list_mail"] == 0   # 一斉配信かは分からない (宛先の人数だけで見分ける)
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
                   "JSON.stringify({ body: text, cc, bcc,",
                   "m.reply_cc_candidates"):
        assert marker in page, marker
    # 差出人名やアドレス (外から来た文字) は textContent / value で入れる
    # 宛先は直せる欄 (HPのフォームから届いたメールで、お客様のアドレスを確かめて直せるように)
    assert '$("m-to").value = ' in page
    assert 'innerHTML = `${m.' not in page
    for field in ("m-to", "m-cc", "m-bcc"):
        assert f'$("{field}").innerHTML' not in page
    # スマホでもアドレスを打ちやすく (自動の大文字化・修正をしない)
    assert page.count('inputmode="email"') >= 2
    # 送信後は宛先・CC・BCC も変えられない
    assert '$("m-cc").readOnly = $("m-bcc").readOnly = $("m-to").readOnly = true;' in page


def test_reply_all_cc_starts_as_the_server_says(client):
    """CC の候補を最初から入れるのは、サーバが reply_all_default で知らせたときだけ.

    (設定「最初から全員に返信にする」がオンで、一斉送信のメールではないとき)。
    送信済みなら送った CC、確認待ち・差し戻しならメール担当者が選んだ CC をそのまま出し
    (空でも候補を入れ直さない)、チェックは CC の欄に合わせる。
    """
    page = client.get("/mail").text
    i = page.index("async function openMail(id)")
    block = page[i:page.index("\n}\n", i)]
    # 前に閉じたときに残した CC・BCC (draft_rcpt_saved) も、確認待ちと同じようにそのまま出す
    assert "const kept = proposed || !!m.draft_rcpt_saved;" in block
    assert ('$("m-cc").value = replied ? addrList(m.reply_cc) : kept ? addrList(m.draft_cc)\n'
            '    : m.reply_all_default ? modal.ccCandidates : "";') in block
    assert '$("m-cc-all").checked = !!$("m-cc").value;' in block
    assert "renderCcNote();" in block
    # BCC は自動では入れない
    assert ('$("m-bcc").value = replied ? addrList(m.reply_bcc) : kept ? addrList(m.draft_bcc) : "";'
            in block)
    assert "modal.savedRcpt = rcptKey();" in block
    # 外すと空に、付け直すと (外す前の欄か) 候補に戻る
    assert 'if ($("m-cc-all").checked) cc.value = modal.ccSaved || modal.ccCandidates;' in page
    assert 'else { modal.ccSaved = cc.value; cc.value = ""; }' in page
    # 一斉送信のメールの知らせ
    assert "一斉送信のメールのため、全員に返信はオフにしています" in page
    assert "!m.reply_all_bulk" in page
    # CC・BCC があるときは、確認の画面で知らせる
    assert "CC・BCC の人にも同じ文が届きます" in page
    assert '(cc.length ? "\\nCC: " + cc.join(", ") : "")' in page


def test_mail_page_send_result_layout(client):
    """送信結果の文はボタンとは別の行に出し、ボタンは折り返さない (iPhone 幅で崩れない).

    0件の CC・BCC は書かない。BCC 欄の案内は iPhone 幅でも切れない短さにする。
    """
    page = client.get("/mail").text
    assert ".modal .bar button { white-space: nowrap; }" in page
    assert "#m-status { order: -1; flex-basis: 100%;" in page
    assert "#m-status:empty { display: none; }" in page
    assert "flex-wrap: wrap" in page[page.index(".modal .bar {"):page.index("}", page.index(".modal .bar {"))]
    assert "BCC ${sentBcc.length}件` : \"\"" in page
    assert "（CC ${sentCc.length}件・BCC ${sentBcc.length}件）" not in page
    assert 'placeholder="任意（ほかの人には見えません）"' in page


def _split_addrs(page: str, values: list[str]) -> list[list[str]]:
    """画面の splitAddrs を node で動かす (node が無ければ飛ばす)."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    i = page.index("const splitAddrs = ")
    code = page[i:page.index("\n};\n", i) + len("\n};\n")]
    script = code + f"\nconsole.log(JSON.stringify({json.dumps(values)}.map(splitAddrs)));"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_split_addrs_keeps_display_names(client):
    """メールソフトからコピーした「名前 <アドレス>」は、名前に空白があっても1件のまま."""
    page = client.get("/mail").text
    out = _split_addrs(page, [
        "山田 太郎 <yamada@example.com>",
        "佐藤 <sato@example.com>, a@x.com、b@y.com;c@z.com\nd@w.com",
        "a@x.com b@y.com",            # 空白だけで区切っても分ける
        "ｅ＠ｘ．ｃｏｍ，　f@y.com",    # 全角は半角に直す
        "  ",
    ])
    assert out == [
        ["山田 太郎 <yamada@example.com>"],
        ["佐藤 <sato@example.com>", "a@x.com", "b@y.com", "c@z.com", "d@w.com"],
        ["a@x.com", "b@y.com"],
        ["e@x.com", "f@y.com"],
        [],
    ]


def test_split_addrs_keeps_commas_inside_names(client):
    """名前の中の「,」(「"姓, 名" <アドレス>」や Outlook の「姓, 名 <アドレス>」) では分けない."""
    page = client.get("/mail").text
    out = _split_addrs(page, [
        '"Yamada, Taro" <taro@example.com>, b@y.com',
        "Yamada, Taro <taro@example.com>; Sato, Hanako <hanako@example.com>",
        "a@x.com, Yamada, Taro <taro@example.com>",
        "Yamada, a@x.com",              # 名前の後が <…> でなければつながない
        '"a,b" <x@y.com>、<c@d.com>',
    ])
    assert out == [
        ['"Yamada, Taro" <taro@example.com>', "b@y.com"],
        ["Yamada, Taro <taro@example.com>", "Sato, Hanako <hanako@example.com>"],
        ["a@x.com", "Yamada, Taro <taro@example.com>"],
        ["Yamada", "a@x.com"],
        ['"a,b" <x@y.com>', "<c@d.com>"],
    ]
    # サーバもそのまま受け付け、アドレスだけで送る
    from server import mail
    cc, _ = mail.check_reply_recipients("z@z.com", out[0] + out[1], [])
    assert cc == ["taro@example.com", "b@y.com", "hanako@example.com"]


def test_send_accepts_display_name_from_screen():
    """画面が送る「名前 <アドレス>」をサーバが受け付け、アドレスだけで送る."""
    from server import mail
    cc, bcc = mail.check_reply_recipients(
        "taro@example.com", ["山田 太郎 <yamada@example.com>"], ["佐藤 <sato@example.com>"])
    assert cc == ["yamada@example.com"] and bcc == ["sato@example.com"]


# ---------- 一部の送り先だけ断られたとき ----------
# smtplib は全員に断られたときだけ例外にし、一部だけなら残りの人に送って
# 断られた人の一覧を返す。以前はそれを見ておらず、宛先 (お客様) に届かず CC にだけ
# 届いたときも「返信済み」になっていた

def test_smtp_send_returns_refused_recipients(monkeypatch):
    from server import notify

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def sendmail(self, frm, to, msg):
            return {"taro@example.com": (550, b"no such user")}

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    refused = notify.smtp_send({"smtp_host": "smtp.e", "smtp_port": "587"},
                               "info@bics.example", ["taro@example.com", "sato@x.com"], "x")
    assert list(refused) == ["taro@example.com"]


def test_send_reply_raises_when_some_refused(monkeypatch):
    from server import mail, notify

    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message: {"sato@x.com": (550, b"no")})
    with pytest.raises(mail.SomeRecipientsRefused) as ei:
        mail.send_reply({"smtp_host": "smtp.e", "mail_from": "info@bics.example"},
                        "taro@example.com", "Re: x", "本文", cc=["sato@x.com"])
    assert ei.value.refused == ["sato@x.com"]
    assert b"Cc: sato@x.com" in ei.value.raw


def _audit_csv(client, admin):
    import datetime as _dt
    return client.get(
        "/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
        headers=auth(admin)).text


def test_send_to_refused_but_cc_delivered_is_not_marked_replied(
        client, users, tmp_path, monkeypatch):
    """宛先 (お客様) が断られ CC にだけ届いたら、未対応に戻して知らせる.

    CC の人には届いているので、送った文の控えは送信済みフォルダに残す。
    """
    from server import mail, notify
    _, admin = users
    _setup(client, admin)
    mid = _add_mail(tmp_path)
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message: {"taro@example.com": (550, b"no")})
    saved = []
    monkeypatch.setattr(mail, "should_save_sent", lambda s: True)
    monkeypatch.setattr(mail, "save_to_sent",
                        lambda s, raw: saved.append(raw) or "Sent")
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "cc": ["sato@x.com"]})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "taro@example.com" in detail and "受け取りを断られました" in detail
    assert "1件には届いています" in detail
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "unhandled" and d["reply_cc"] == []
    csv_text = _audit_csv(client, admin)
    assert "refused=taro@example.com" in csv_text and "delivered=sato@x.com" in csv_text
    assert "saved=Sent" in csv_text
    assert "メール返信送信（宛先に届かず）" in csv_text   # 修正履歴では日本語で出す
    assert len(saved) == 1 and b"Cc: sato@x.com" in saved[0]
    copy = email.message_from_bytes(saved[0], policy=email.policy.default)
    assert "本文" in copy.get_body(("plain",)).get_content()


def test_send_to_refused_and_nobody_delivered_saves_no_copy(
        client, users, tmp_path, monkeypatch):
    """誰にも届いていなければ、送信済みフォルダに控えを残さない."""
    from server import mail, notify
    _, admin = users
    _setup(client, admin)
    mid = _add_mail(tmp_path)
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message: {"taro@example.com": (550, b"no")})
    saved = []
    monkeypatch.setattr(mail, "should_save_sent", lambda s: True)
    monkeypatch.setattr(mail, "save_to_sent",
                        lambda s, raw: saved.append(raw) or "Sent")
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 502
    assert saved == []


def test_send_with_some_cc_refused_reports_them(client, users, tmp_path, monkeypatch):
    """CC・BCC の一部だけ断られたら、返信済みにして、届かなかった人を知らせる."""
    from server import notify
    _, admin = users
    _setup(client, admin)
    mid = _add_mail(tmp_path)
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message: {"Bad@x.com": (550, b"no")})
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "cc": ["bad@x.com", "ok@y.jp"],
                          "bcc": ["boss@bics.example"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refused"] == ["Bad@x.com"]
    assert body["cc"] == ["ok@y.jp"] and body["bcc"] == ["boss@bics.example"]
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "replied" and d["reply_cc"] == ["ok@y.jp"]
    csv_text = _audit_csv(client, admin)
    assert "refused=Bad@x.com" in csv_text and "recipients=3" in csv_text
    # 画面で届かなかったアドレスを出す
    assert "次のアドレスには届きませんでした" in client.get("/mail").text


# ---------- 最初から全員に返信 (設定 mail_reply_all_default) ----------

def _staff(tmp_path, name="sato-staff"):
    """メール担当者 (スタッフ) を1人作る."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        u = db.create_user(conn, name)
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (u["id"],))
    return u


def test_reply_all_default_setting_is_on_and_admin_only(client, users, tmp_path):
    worker, admin = users
    staff = _staff(tmp_path)
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_reply_all_default"] == 1          # 最初はオン
    for u in (worker, staff):                         # 管理者だけが変えられる
        r = client.patch("/api/settings", headers=auth(u),
                         json={"mail_reply_all_default": False})
        assert r.status_code == 403
        assert client.get("/api/settings", headers=auth(u)).status_code == 403
    # はい・いいえ以外は受け付けない
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_reply_all_default": "たぶん"})
    assert r.status_code == 422
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_reply_all_default": False})
    assert r.status_code == 200 and r.json()["mail_reply_all_default"] == 0
    assert client.get("/api/settings", headers=auth(admin)).json()[
        "mail_reply_all_default"] == 0
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_reply_all_default": True})
    assert r.json()["mail_reply_all_default"] == 1


def test_detail_reply_all_default_on_and_off(client, users, tmp_path):
    _, admin = users
    staff = _staff(tmp_path)
    _setup(client, admin, mail_from="info@bics.example", imap_user="info@bics.example")
    mid = _add_mail_with_cc(tmp_path, "info@bics.example, sato@x.com", "suzuki@y.jp")
    for u in (admin, staff):   # メール担当者にも同じに見える
        d = client.get(f"/api/mail/{mid}", headers=auth(u)).json()
        assert d["reply_cc_candidates"] == ["sato@x.com", "suzuki@y.jp"]
        assert d["reply_all_default"] is True and d["reply_all_bulk"] is False
    # 相手 (自分たちだけ宛て) のほかに誰もいないメールは、オンでも全員に返信にしない
    solo = _add_mail_with_cc(tmp_path, "info@bics.example", "", n=2,
                             message_id="<solo@example.com>")
    d = client.get(f"/api/mail/{solo}", headers=auth(admin)).json()
    assert d["reply_cc_candidates"] == [] and d["reply_all_default"] is False
    # オフにすると、候補は出す (チェックを付ければ入る) が最初は付けない
    _setup(client, admin, mail_reply_all_default=False)
    for u in (admin, staff):
        d = client.get(f"/api/mail/{mid}", headers=auth(u)).json()
        assert d["reply_cc_candidates"] == ["sato@x.com", "suzuki@y.jp"]
        assert d["reply_all_default"] is False and d["reply_all_bulk"] is False


def test_reply_all_cc_leaves_out_us_staff_forms_and_the_person_we_reply_to(
        client, users, tmp_path):
    from server import db
    worker, admin = users
    _setup(client, admin, mail_from="株式会社インビックス <info@bics.example>",
           smtp_user="smtp@bics.example", imap_user="info@bics.example")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        # 2つ目の受信アドレス (kurosu@) と、社内の人のメールアドレス
        conn.execute("INSERT INTO mail_accounts (label, imap_user, from_addr, created_at)"
                     " VALUES (?, ?, ?, ?)", ("黒須", "kurosu@bics.example",
                                              "kurosu@bics.example", "2026-01-01T00:00:00+00:00"))
        conn.execute("UPDATE users SET email = 'Tanaka@Bics.example' WHERE id = ?",
                     (worker["id"],))
    mid = _add_mail_with_cc(
        tmp_path,
        "info@bics.example, KUROSU@bics.example, sato@x.com, noreply@x.com",
        "smtp@bics.example, tanaka@bics.example, wordpress@shop.example, "
        "taro@example.com, 経理 <keiri@x.com>")
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    # 自分たち (受信アドレス全部・差出人・SMTP)・社内の人・フォームの送信元・返信の相手は入らない
    assert d["reply_cc_candidates"] == ["sato@x.com", "keiri@x.com"]
    assert d["reply_all_default"] is True


def test_reply_all_cc_leaves_out_the_reply_to_contact():
    """Reply-To (お客様) に返すときは、そのお客様も CC に入れない (2通届かないように)."""
    from server import mail

    m = {"from_addr": "wp@shop.example", "contact_source": "reply_to",
         "contact_addr": "Guest@Example.com", "contact_name": "",
         "to_addr": "info@bics.example, guest@example.com", "cc_addr": "a@x.com"}
    assert mail.reply_all_cc(OWN, m) == ["a@x.com"]
    # 確認をお願いしたときに直した宛先も入れない
    m2 = {"from_addr": "a@example.com", "status": "pending_approval",
          "draft_to": "b@example.com", "to_addr": "b@example.com, c@example.com"}
    assert mail.reply_all_cc(OWN, m2) == ["c@example.com"]
    # 社内の人 (people) は入れない (大文字・小文字は区別しない)
    m3 = {"from_addr": "a@example.com", "to_addr": "Boss@bics.example, c@example.com"}
    assert mail.reply_all_cc(OWN, m3, people=["boss@BICS.example"]) == ["c@example.com"]


def test_parse_message_marks_list_mail():
    from server import mail

    base = (b"From: a@example.com\r\nTo: list@example.com, b@example.com\r\n"
            b"Subject: x\r\n")
    assert mail.parse_message(base + b"\r\nbody\r\n")["list_mail"] == 0
    for h in (b"List-Id: <news.example.com>\r\n",
              b"List-Unsubscribe: <mailto:off@example.com>\r\n",
              b"list-id: <lower.example.com>\r\n"):
        assert mail.parse_message(base + h + b"\r\nbody\r\n")["list_mail"] == 1


def test_reply_all_bulk_by_headers_or_count():
    from server import mail

    to = ", ".join(f"u{i}@example.com" for i in range(6))
    cc = ", ".join(f"c{i}@example.com" for i in range(4))
    assert mail.REPLY_ALL_BULK_OVER == 10
    # 10人まではふつうのメール (同じ人の重複・大文字の違いは1人)
    assert not mail.reply_all_bulk({"to_addr": to, "cc_addr": cc + ", U0@example.com"})
    assert mail.reply_all_bulk({"to_addr": to, "cc_addr": cc + ", c4@example.com"})
    # List-Id などがあったメールは、人数が少なくても一斉送信
    assert mail.reply_all_bulk({"to_addr": "ml@example.com", "list_mail": 1})
    assert not mail.reply_all_bulk({"to_addr": "", "cc_addr": ""})


def test_detail_bulk_mail_is_not_reply_all_by_default(client, users, tmp_path):
    from server import db
    _, admin = users
    staff = _staff(tmp_path)
    _setup(client, admin, mail_from="info@bics.example", imap_user="info@bics.example")
    many = ", ".join(f"u{i}@example.com" for i in range(11))
    mid = _add_mail_with_cc(tmp_path, "info@bics.example", many)
    for u in (admin, staff):
        d = client.get(f"/api/mail/{mid}", headers=auth(u)).json()
        assert len(d["reply_cc_candidates"]) == 11
        assert d["reply_all_default"] is False and d["reply_all_bulk"] is True
    # メーリングリスト (List-Id があった) は、宛先が少なくても同じ
    ml = _add_mail_with_cc(tmp_path, "ml@lists.example, info@bics.example", "", n=2,
                           message_id="<ml@example.com>")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET list_mail = 1 WHERE id = ?", (ml,))
    d = client.get(f"/api/mail/{ml}", headers=auth(admin)).json()
    assert d["reply_cc_candidates"] == ["ml@lists.example"]
    assert d["reply_all_default"] is False and d["reply_all_bulk"] is True
    # 設定がオフなら「一斉送信のため」の知らせも出さない (もともと付けない)
    _setup(client, admin, mail_reply_all_default=False)
    d = client.get(f"/api/mail/{ml}", headers=auth(admin)).json()
    assert d["reply_all_default"] is False and d["reply_all_bulk"] is False


def test_fetch_stores_list_mail(tmp_path, monkeypatch):
    from server import db, mail

    def raw_mail(n, extra):
        msg = EmailMessage()
        msg["From"] = "news@example.com"
        msg["To"] = "info@bics.example, b@example.com"
        msg["Subject"] = "お知らせ"
        msg["Message-ID"] = f"<list{n}@example.com>"
        for k, v in extra.items():
            msg[k] = v
        msg.set_content("本文")
        return msg.as_bytes()

    raws = {b"5": raw_mail(1, {"List-Id": "<news.example.com>"}), b"6": raw_mail(2, {})}

    class FakeIMAP:
        def select(self, folder, readonly=False):
            return ("OK", [b"2"])

        def response(self, key):
            return ("UIDVALIDITY", [b"1"])

        def uid(self, cmd, *args):
            if cmd == "search":
                return ("OK", [b"5 6"])
            uid = args[0].encode() if isinstance(args[0], str) else args[0]
            return ("OK", [(b"header", raws[uid])])

        def logout(self):
            pass

    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP())
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "mail_auto_draft": 0}
        assert len(mail.fetch_new_mail(conn, s)) == 2
        rows = dict(conn.execute("SELECT message_id, list_mail FROM mails").fetchall())
    assert rows == {"<list1@example.com>": 1, "<list2@example.com>": 0}


def test_staff_draft_cc_is_kept_for_the_admin(client, users, tmp_path, monkeypatch):
    """メール担当者が CC を外して確認をお願いしたら、管理者にもそのまま (空のまま) 見える.

    全員に返信が最初からオンでも、頼んだときの CC を入れ直さない (画面は draft_cc を出す)。
    """
    from server import app as app_module
    _, admin = users
    staff = _staff(tmp_path)
    _setup(client, admin, mail_from="info@bics.example", imap_user="info@bics.example")
    monkeypatch.setattr(app_module.notify, "deliver_async", lambda *a, **kw: None)
    mid = _add_mail_with_cc(tmp_path, "info@bics.example, sato@x.com", "suzuki@y.jp")
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                    json={"body": "返信です", "cc": [], "bcc": []})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "pending_approval"
    assert d["draft_cc"] == [] and d["draft_bcc"] == []
    # 1人だけ残して頼み直した場合も、そのとおり
    mid2 = _add_mail_with_cc(tmp_path, "info@bics.example, sato@x.com", "suzuki@y.jp",
                             n=2, message_id="<m2b@example.com>")
    r = client.post(f"/api/mail/{mid2}/request-approval", headers=auth(staff),
                    json={"body": "返信です", "cc": ["suzuki@y.jp"], "bcc": ["boss@x.jp"]})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/mail/{mid2}", headers=auth(admin)).json()
    assert d["draft_cc"] == ["suzuki@y.jp"] and d["draft_bcc"] == ["boss@x.jp"]


# ---------- 書きかけの CC・BCC を残す・確認に同じ人を2回出さない (レビューの指摘) ----------

def test_draft_cc_and_bcc_are_kept_when_the_reply_screen_is_closed(client, users, tmp_path):
    """返信画面を閉じたときに、書きかけの CC・BCC も残す (開き直したときに、外した人を
    「全員に返信」で入れ直さない)。空にした (チェックを外した) ことも残す."""
    _, admin = users
    staff = _staff(tmp_path)
    _setup(client, admin, mail_from="info@bics.example", imap_user="info@bics.example")
    mid = _add_mail_with_cc(tmp_path, "info@bics.example, bucho@x.jp", "keiri@x.jp")
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["reply_all_default"] and d["draft_rcpt_saved"] is False
    assert d["reply_cc_candidates"] == ["bucho@x.jp", "keiri@x.jp"]
    # 返信文だけの保存 (前の画面) では、CC・BCC は残したことにならない
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_reply": "書きかけ", "seen_request": ""})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["draft_rcpt_saved"] is False
    # keiri@ を外して閉じた
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_reply": "書きかけの返信文", "draft_cc": " bucho@x.jp ",
                           "draft_bcc": "", "seen_request": ""})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["draft_rcpt_saved"] is True
    assert (d["draft_reply"], d["draft_cc"], d["draft_bcc"]) == ("書きかけの返信文", ["bucho@x.jp"], [])
    # チェックを外して (CC を空にして) 閉じた
    r = client.patch(f"/api/mail/{mid}", headers=auth(staff),
                     json={"draft_reply": "書きかけの返信文", "draft_cc": "",
                           "draft_bcc": "boss@x.jp", "seen_request": ""})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/mail/{mid}", headers=auth(staff)).json()
    assert d["draft_rcpt_saved"] is True and d["draft_cc"] == [] and d["draft_bcc"] == ["boss@x.jp"]
    # 改行は入れない・長すぎるものは断る
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_cc": "a@x.jp\nb@x.jp", "seen_request": ""})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["draft_cc"] == ["a@x.jp b@x.jp"]
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_cc": "a@x.jp, " * 2000, "seen_request": ""})
    assert r.status_code == 400 and "CC" in r.json()["detail"]
    # 書いたことは操作の記録 (状態の変更) には出さない
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'mail_update'").fetchone()


def test_staff_cannot_change_draft_cc_while_pending(client, users, tmp_path, monkeypatch):
    """確認待ちの間は、メール担当者は CC・BCC も変えられない (管理者が見ている内容なので)."""
    from server import app as app_module
    _, admin = users
    staff = _staff(tmp_path)
    _setup(client, admin, mail_from="info@bics.example", imap_user="info@bics.example")
    monkeypatch.setattr(app_module.notify, "deliver_async", lambda *a, **kw: None)
    mid = _add_mail_with_cc(tmp_path, "info@bics.example, bucho@x.jp", "keiri@x.jp")
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                    json={"body": "返信です", "cc": ["keiri@x.jp"], "bcc": []})
    assert r.status_code == 200, r.text
    req_at = r.json()["requested_at"]
    r = client.patch(f"/api/mail/{mid}", headers=auth(staff),
                     json={"draft_cc": "", "seen_request": req_at})
    assert r.status_code == 409
    assert client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["draft_cc"] == ["keiri@x.jp"]
    # 画面を開いた後に確認をお願いされていたら、管理者の分も上書きしない
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_cc": "", "seen_request": ""})
    assert r.status_code == 409
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_cc": "bucho@x.jp", "seen_request": req_at})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["draft_cc"] == ["bucho@x.jp"]


def test_mail_page_keeps_draft_cc_and_dedupes_the_confirm(client):
    """閉じるときに CC・BCC も残し、送る前の確認・確認のお願いでは宛先と同じ人を CC・BCC から除く."""
    page = client.get("/mail").text
    i = page.index("async function saveDraft(")
    block = page[i:page.index("\n}\n", i)]
    assert "const rcptChanged = rcpt !== modal.savedRcpt;" in block
    assert 'Object.assign(body, { draft_cc: $("m-cc").value, draft_bcc: $("m-bcc").value });' in block
    # 残したことは、保存を頼んだときに開いていたメールの印に残す (待っている間に閉じても、次のメールに付けない)
    assert "opened = modal;" in block and "opened.savedRcpt = rcpt;" in block
    dedupe = ('const [cc, bcc] = dedupeRcpts(to, splitAddrs($("m-cc").value), '
              'splitAddrs($("m-bcc").value));')
    for handler in ('$("m-send").onclick', '$("m-request").onclick'):
        i = page.index(handler)
        block = page[i:page.index("\n};\n", i)]
        assert dedupe in block, handler
        assert block.index(dedupe) < block.index("confirm("), handler
