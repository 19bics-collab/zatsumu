"""送信箱のテスト.

- 送信済みフォルダの取り込み (読むだけ・差分だけ・この画面から送った返信の控えは二重に出さない)
- 届いたメールへの返信なら、未対応のときだけ自動で返信済みにする (ヘッダでつながるもの・
  ヘッダが無いときは宛先と件名で1通に決まるものだけ。当て推量はしない)
- 送信済みだけ取り込むアドレス (Gmail など。受信箱には入れない)・Gmail の日本語のフォルダ名
- 1つのアドレスの失敗はほかに広がらない・前の版のコードが新しい DB で動く
- 見られる人 (管理者・メール担当者)・設定を変えられる人 (管理者だけ)・画面

ネットワークには出ない (IMAP・SMTP は偽物に差し替える)。
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

ROOT = Path(__file__).resolve().parent.parent
# 本番で今動いている版 (自動更新で戻ることがある版)
PREVIOUS = "aa00391"
GMAIL_SENT = "[Gmail]/&kAFP4W4IMH8w4TD8MOs-"      # [Gmail]/送信済みメール


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
        staff = db.create_user(conn, "suzuki")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        admin = db.create_user(conn, "boss", is_admin=True)
        db.set_setting(conn, "mail_auto_draft", 0)
        db.set_setting(conn, "smtp_host", "smtp.example.com")
    return worker, staff, admin


def sent_raw(n, to="taro@example.com", subject="Re: 件名1", in_reply_to="", references="",
             cc="", frm="info@bics.example", date="Wed, 28 Oct 2026 10:00:00 +0900",
             body="お世話になっております。\nご連絡ありがとうございます。", attach=None):
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<w{n}@webmail.example>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if attach:
        msg.add_attachment(attach[1], maintype="application", subtype="pdf", filename=attach[0])
    return msg.as_bytes()


class Server:
    """1つのメールボックス (フォルダ名 → {UID: メール})。読むだけの操作と、返信の控えの保存だけ持つ.

    STORE・EXPUNGE・COPY などは持たない (呼ばれたら AttributeError になり、テストが落ちる)。
    """

    def __init__(self, folders=None, sent=("Sent", "\\Sent"), fail=None):
        self.folders = {"INBOX": {}}
        self.sent_name, flag = sent if sent else (None, None)
        self.flags = {}
        if self.sent_name:
            self.folders[self.sent_name] = {}
            self.flags[self.sent_name] = flag
        for k, v in (folders or {}).items():
            self.folders.setdefault(k, {}).update(v)
        self.fail = fail
        self.selected = None
        self.searches = []
        self.uidvalidity = {k: b"5" for k in self.folders}

    def list(self, *args):
        lines = []
        for name in self.folders:
            flags = "\\HasNoChildren" + (f" {self.flags[name]}" if self.flags.get(name) else "")
            lines.append(f'({flags}) "/" "{name}"'.encode())
        return ("OK", lines)

    def select(self, folder, readonly=False):
        assert readonly, "メールボックスは読むだけで開く"
        name = folder[1:-1] if folder.startswith('"') else folder
        if name not in self.folders:
            return ("NO", [b"no such mailbox"])
        self.selected = name
        return ("OK", [str(len(self.folders[name])).encode()])

    def response(self, key):
        return ("UIDVALIDITY", [self.uidvalidity[self.selected]])

    def uid(self, cmd, *args):
        box = self.folders[self.selected]
        if cmd == "search":
            self.searches.append((self.selected, args[1]))
            crit = args[1]
            uids = sorted(box)
            if crit.startswith("(UID "):
                low = int(crit[5:].split(":")[0])
                uids = [u for u in uids if u >= low] or uids[-1:]
            return ("OK", [b" ".join(str(u).encode() for u in uids)])
        return ("OK", [(b"header", box[int(args[0])])])

    def append(self, mailbox, flags, date_time, message):
        name = mailbox[1:-1]
        box = self.folders[name]
        box[max(box, default=0) + 1] = message if isinstance(message, bytes) else message.encode()
        return ("OK", [b"APPEND done"])

    def status(self, mailbox, names):
        return ("OK", [b"x"])

    def logout(self):
        pass


def use_servers(monkeypatch, servers):
    from server import mail
    logins = []

    def connect(s):
        logins.append(s["imap_host"])
        srv = servers[s["imap_host"]]
        if srv.fail:
            raise srv.fail
        return srv

    monkeypatch.setattr(mail, "_imap_connect", connect)
    return logins


INFO = {"from_addr": "info@bics.example", "imap_host": "imap.bics.example",
        "imap_user": "info@bics.example", "imap_pass": "info-secret-pw", "label": "info"}
GMAIL = {"from_addr": "kurosu.bics@gmail.com", "imap_host": "imap.gmail.com",
         "imap_user": "kurosu.bics@gmail.com", "imap_pass": "gmail-app-pw", "label": "Gmail",
         "sent_only": True}


def add_mail(path, n=1, account=1, **kw):
    """届いたメールを1通入れる (info@ に届いたもの)."""
    from server import db
    mail_id = _add_mail(path, n=n, **kw)
    with db.get_db(path / "zatsumu.db") as conn:
        if conn.execute("SELECT 1 FROM mail_accounts WHERE id = ?", (account,)).fetchone():
            conn.execute("UPDATE mails SET account_id = ? WHERE id = ?", (account, mail_id))
    return mail_id


def add_account(client, admin, **fields):
    r = client.post("/api/mail/accounts", headers=auth(admin), json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def fetch(client, admin):
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 200, r.text
    return r.json()


def row(tmp_path, sql, params=()):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def rows(tmp_path, sql, params=()):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def sent_list(client, user, **params):
    r = client.get("/api/mail/sent", headers=auth(user), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def mail_status(tmp_path, mail_id):
    return row(tmp_path, "SELECT * FROM mails WHERE id = ?", (mail_id,))


# ---------- 取り込み (差分だけ・この画面から送った返信は二重に出さない) ----------

def test_import_is_incremental_and_skips_copies_of_app_replies(client, users, tmp_path,
                                                               monkeypatch):
    from server import notify
    _, staff, admin = users
    acc = add_account(client, admin, **INFO)
    m1 = add_mail(tmp_path, n=1)
    m2 = add_mail(tmp_path, n=2, from_addr="hanako@example.com", subject="見積もり")
    srv = Server()
    use_servers(monkeypatch, {"imap.bics.example": srv})
    sent = []
    monkeypatch.setattr(notify, "smtp_send", lambda s, frm, rcpts, raw: sent.append(raw) or {})
    # この画面から返信 (控えは送信済みフォルダに保存される)
    r = client.post(f"/api/mail/{m1}/send", headers=auth(admin), json={"body": "画面からの返信"})
    assert r.status_code == 200 and r.json()["saved_to_sent"], r.text
    assert len(srv.folders["Sent"]) == 1
    app_msgid = mail_status(tmp_path, m1)["reply_message_id"]
    assert app_msgid and app_msgid.encode() in srv.folders["Sent"][1]
    # Webメールから送ったメール (返信ではない新しいメール)
    srv.folders["Sent"][2] = sent_raw(1, to="new@other.example", subject="ご挨拶")
    fetch(client, admin)
    got = rows(tmp_path, "SELECT * FROM sent_mails")
    assert [g["message_id"] for g in got] == ["<w1@webmail.example>"]   # 控えは取り込まない
    assert got[0]["folder"] == "Sent" and got[0]["source"] == "webmail"
    assert got[0]["account_id"] == acc["id"] and got[0]["imap_uid"] == 2
    a = row(tmp_path, "SELECT * FROM mail_accounts WHERE id = ?", (acc["id"],))
    assert (a["sent_state_folder"], a["sent_state_uidvalidity"], a["sent_state_last_uid"]) == \
        ("Sent", "5", 2)
    assert a["sent_fetch_ok_at"] and a["sent_fetch_error"] == ""
    # 送信箱には両方 (新しい順)。メール担当者も見られる
    lst = sent_list(client, staff)
    assert lst["total"] == 2
    keys = [i["key"] for i in lst["items"]]
    assert set(keys) == {f"a{m1}", f"i{got[0]['id']}"}
    app_item = next(i for i in lst["items"] if i["kind"] == "app")
    assert (app_item["to_addr"], app_item["source_label"], app_item["account_label"]) == \
        ("taro@example.com", "この画面", "info")
    assert app_item["first_line"] == "画面からの返信" and app_item["mail_id"] == m1
    imp = next(i for i in lst["items"] if i["kind"] == "imported")
    assert (imp["to_addr"], imp["source_label"], imp["subject"], imp["first_line"]) == \
        ("new@other.example", "Webメールなど", "ご挨拶",
         "お世話になっております。 ご連絡ありがとうございます。")
    # 2回目は続きから (取り込み済みのものは読み直さない)
    srv.searches.clear()
    srv.folders["Sent"][3] = sent_raw(2, to="hanako@example.com", subject="Re: 見積もり",
                                      in_reply_to="<m2@example.com>")
    fetch(client, admin)
    assert ("Sent", "(UID 3:*)") in srv.searches
    assert len(rows(tmp_path, "SELECT id FROM sent_mails")) == 2
    assert mail_status(tmp_path, m2)["status"] == "replied"
    # 3回目は何も増えない (最後の1通を返す「UID n:*」でも二重に入らない)
    fetch(client, admin)
    assert len(rows(tmp_path, "SELECT id FROM sent_mails")) == 2
    assert sent_list(client, admin)["total"] == 3


def test_sent_detail_shows_full_text_recipients_and_attachments(client, users, tmp_path,
                                                                 monkeypatch):
    _, staff, admin = users
    add_account(client, admin, **INFO)
    m = add_mail(tmp_path, n=1, from_addr="tanaka@abc.example.jp", from_name="田中")
    long_body = "1行目\n" + "長い本文です。" * 500
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(
        1, to="田中 <tanaka@abc.example.jp>, sato@abc.example.jp", cc="keiri@abc.example.jp",
        in_reply_to="<m1@example.com>", body=long_body, attach=("見積書.pdf", b"%PDF-1.4 x"))
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    sid = row(tmp_path, "SELECT id FROM sent_mails")["id"]
    d = client.get(f"/api/mail/sent/i{sid}", headers=auth(staff)).json()
    assert d["body"].startswith("1行目") and len(d["body"]) > 3000
    assert d["to"] == [{"name": "田中", "addr": "tanaka@abc.example.jp"},
                       {"name": "", "addr": "sato@abc.example.jp"}]
    assert d["cc"] == [{"name": "", "addr": "keiri@abc.example.jp"}] and d["bcc"] == []
    assert d["mail"]["id"] == m and d["marked_replied"] and d["link_kind"] == "header"
    assert d["client"] and d["client"]["key"] == "d:abc.example.jp"
    assert [a["filename"] for a in d["attachments"]] == ["見積書.pdf"]
    att = d["attachments"][0]
    r = client.get(f"/api/mail/sent/i{sid}/attachments/{att['id']}", headers=auth(staff))
    assert r.status_code == 200 and r.content == b"%PDF-1.4 x"
    assert "attachment" in r.headers["content-disposition"]
    # 別のメールの添付・形の違う番号は 404
    assert client.get(f"/api/mail/sent/i{sid + 1}/attachments/{att['id']}",
                      headers=auth(staff)).status_code == 404
    assert client.get(f"/api/mail/sent/a{m}/attachments/{att['id']}",
                      headers=auth(staff)).status_code == 404
    for bad in ("x1", "i0", "a", "i1x", f"a{m}"):   # a{m} は自動で返信済みにしたので「この画面」ではない
        assert client.get(f"/api/mail/sent/{bad}", headers=auth(staff)).status_code == 404, bad
    # この画面から送った返信は BCC も出す
    m2 = add_mail(tmp_path, n=2)
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET status = 'replied', replied_at = '2026-10-28T00:00:00+00:00',"
                     " replied_by = 'boss', reply_body = '本文', reply_bcc = 'boss@bics.example',"
                     " reply_cc = 'c@x.example' WHERE id = ?", (m2,))
    d = client.get(f"/api/mail/sent/a{m2}", headers=auth(staff)).json()
    assert d["bcc"] == [{"name": "", "addr": "boss@bics.example"}]
    assert d["cc"] == [{"name": "", "addr": "c@x.example"}] and d["sent_by"] == "boss が送信"
    assert d["mail"]["id"] == m2 and d["source_label"] == "この画面"


# ---------- 自動で返信済み ----------

def test_header_reply_marks_unhandled_mail_replied(client, users, tmp_path, monkeypatch):
    _, staff, admin = users
    add_account(client, admin, **INFO)
    m = add_mail(tmp_path, n=1, references_hdr="")
    before = client.get("/api/mail/status", headers=auth(admin)).json()
    srv = Server()
    srv.folders["Sent"][7] = sent_raw(1, in_reply_to="<m1@example.com>",
                                      references="<m1@example.com>", cc="boss@other.example",
                                      body="Webメールからの返信です")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    r = mail_status(tmp_path, m)
    sid = row(tmp_path, "SELECT id FROM sent_mails")["id"]
    assert r["status"] == "replied" and r["replied_by"] == "Webメール"
    assert r["reply_source"] == "webmail" and r["reply_sent_id"] == sid
    assert r["reply_body"] == "Webメールからの返信です\n" or r["reply_body"].startswith("Webメールから")
    assert r["reply_subject"] == "Re: 件名1" and r["reply_sent_to"] == "taro@example.com"
    assert r["reply_cc"] == "boss@other.example"
    assert r["replied_at"].startswith("2026-10-28T01:00:00")
    assert r["reply_message_id"] == "<w1@webmail.example>"
    # みんなの画面に出る (未対応の数が減るので、開いたままの画面も取り直す)
    after = client.get("/api/mail/status", headers=auth(staff)).json()
    assert after["unhandled"] == before["unhandled"] - 1
    assert after["sent_mark"] != before["sent_mark"]
    d = client.get(f"/api/mail/{m}", headers=auth(staff)).json()
    assert (d["reply_source"], d["reply_source_label"], d["reply_sent_key"]) == \
        ("webmail", "Webメールなど", f"i{sid}")
    assert d["reply_body"].startswith("Webメールからの返信です")
    # 操作の記録 (操作者は「（自動）」)
    log = row(tmp_path, "SELECT * FROM audit_log WHERE action = 'mail_auto_replied'")
    assert log["admin_id"] == 0 and f"mail={m} sent={sid} via=webmail" in log["detail"]
    csv = client.get("/api/reports/audit.csv?month=" + log["at"][:7], headers=auth(admin)).text
    assert "（自動）" in csv and "Webメールなどからの返信で返信済みにした" in csv
    # 送信箱の一覧: 取り込んだ1通だけ (自動で返信済みにしたメールを「この画面」に二重に出さない)
    lst = sent_list(client, admin)
    assert [i["key"] for i in lst["items"]] == [f"i{sid}"]
    assert lst["items"][0]["marked_replied"] and lst["items"][0]["mail_subject"] == "件名1"
    # 相手がこの返信に返事をくれたら、同じやり取りだと分かる
    m3 = add_mail(tmp_path, n=3, references_hdr="<w1@webmail.example>")
    from server import db, mail
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_refs(conn, m3, "<w1@webmail.example>", "")
        assert m in {t["id"] for t in mail.mail_thread(conn, conn.execute(
            "SELECT * FROM mails WHERE id = ?", (m3,)).fetchone())["thread"]}


def test_reply_by_references_only_and_follow_up_to_own_reply(client, users, tmp_path,
                                                             monkeypatch):
    _, _, admin = users
    add_account(client, admin, **INFO)
    m1 = add_mail(tmp_path, n=1, status="replied")
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET reply_message_id = '<app1@bics.example>' WHERE id = ?", (m1,))
    m2 = add_mail(tmp_path, n=2)
    srv = Server()
    # In-Reply-To が無く、References の新しい方 (最後) が m2
    srv.folders["Sent"][1] = sent_raw(1, references="<m1@example.com> <app1@bics.example> <m2@example.com>")
    # こちらの返信 (m1 への) への続き: m1 につなぐが、返信済みのままで変えない
    srv.folders["Sent"][2] = sent_raw(2, in_reply_to="<app1@bics.example>")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    got = {g["message_id"]: g for g in rows(tmp_path, "SELECT * FROM sent_mails")}
    assert got["<w1@webmail.example>"]["linked_mail_id"] == m2
    assert got["<w1@webmail.example>"]["marked_replied"] == 1
    assert got["<w2@webmail.example>"]["linked_mail_id"] == m1
    assert got["<w2@webmail.example>"]["marked_replied"] == 0
    assert mail_status(tmp_path, m2)["status"] == "replied"
    assert mail_status(tmp_path, m1)["reply_source"] == ""   # この画面から送った返信のまま


def test_never_changes_mails_that_are_not_unhandled(client, users, tmp_path, monkeypatch):
    _, _, admin = users
    add_account(client, admin, **INFO)
    ids = {st: add_mail(tmp_path, n=i, status=st)
           for i, st in enumerate(("replied", "archived", "pending_approval"), start=1)}
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET replied_by = '管理者', reply_body = '前の返信',"
                     " replied_at = '2026-08-12T00:00:00+00:00' WHERE id = ?", (ids["replied"],))
    before = {st: mail_status(tmp_path, i) for st, i in ids.items()}
    srv = Server()
    for n, mid in enumerate(ids.values(), start=1):
        srv.folders["Sent"][n] = sent_raw(n, in_reply_to=f"<m{n}@example.com>",
                                          subject=f"Re: 件名{n}")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    for st, mid in ids.items():
        assert mail_status(tmp_path, mid) == before[st], st
    got = rows(tmp_path, "SELECT linked_mail_id, marked_replied FROM sent_mails ORDER BY id")
    assert [(g["linked_mail_id"], g["marked_replied"]) for g in got] == \
        [(i, 0) for i in ids.values()]
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'mail_auto_replied'")["n"] == 0


def test_subject_fallback_marks_only_when_exactly_one_mail_matches(client, users, tmp_path,
                                                                   monkeypatch):
    _, _, admin = users
    add_account(client, admin, **INFO)
    m = add_mail(tmp_path, n=1, from_addr="Hanako@Example.com", subject="お見積もりの件")
    srv = Server()
    # ヘッダが無い (携帯のメールなどで新しく書いた) が、宛先が相手で件名が同じ (RE: と全角コロン)
    srv.folders["Sent"][1] = sent_raw(1, to="hanako@example.com", subject="RE：Re: お見積もりの件")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    r = mail_status(tmp_path, m)
    assert r["status"] == "replied" and r["reply_source"] == "webmail"
    s = row(tmp_path, "SELECT * FROM sent_mails")
    assert (s["linked_mail_id"], s["link_kind"], s["marked_replied"]) == (m, "subject", 1)


@pytest.mark.parametrize("case", [
    "two_matching_mails", "subject_differs", "sent_before_mail", "counterpart_only_in_cc",
    "forward", "other_address", "mail_archived", "header_points_elsewhere_but_no_match_twice"])
def test_subject_fallback_does_not_guess(client, users, tmp_path, monkeypatch, case):
    _, _, admin = users
    add_account(client, admin, **INFO)
    kw = dict(to="hanako@example.com", subject="Re: お見積もりの件")
    m = add_mail(tmp_path, n=1, from_addr="hanako@example.com", subject="お見積もりの件",
                  status="archived" if case == "mail_archived" else "unhandled")
    if case in ("two_matching_mails", "header_points_elsewhere_but_no_match_twice"):
        add_mail(tmp_path, n=2, from_addr="hanako@example.com", subject="Re: お見積もりの件")
    if case == "subject_differs":
        kw["subject"] = "Re: 請求書の件"
    if case == "sent_before_mail":
        kw["date"] = "Mon, 10 Aug 2026 09:00:00 +0900"   # 届く前 (2026-08-11)
    if case == "counterpart_only_in_cc":
        kw.update(to="boss@other.example", cc="hanako@example.com")
    if case == "forward":
        kw["subject"] = "Fw: お見積もりの件"
    if case == "other_address":
        kw["to"] = "jiro@example.com"
    if case == "header_points_elsewhere_but_no_match_twice":
        kw["in_reply_to"] = "<unknown@elsewhere.example>"
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(1, **kw)
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    assert all(r["status"] in ("unhandled", "archived") and r["reply_source"] == ""
               for r in rows(tmp_path, "SELECT * FROM mails"))
    s = row(tmp_path, "SELECT * FROM sent_mails")
    assert s["marked_replied"] == 0 and s["linked_mail_id"] is None, case
    assert mail_status(tmp_path, m)["status"] == ("archived" if case == "mail_archived"
                                                  else "unhandled")


def test_forward_and_mails_only_to_ourselves_are_linked_but_not_marked(client, users, tmp_path,
                                                                       monkeypatch):
    _, _, admin = users
    add_account(client, admin, **INFO)
    add_account(client, admin, from_addr="kurosu@bics.example", imap_host="imap.bics.example",
                imap_user="kurosu@bics.example", imap_pass="pw", imap_folder="INBOX.kurosu",
                import_sent=False, label="黒須")
    m1 = add_mail(tmp_path, n=1)
    m2 = add_mail(tmp_path, n=2)
    srv = Server(folders={"INBOX.kurosu": {}})
    # Gmail などは転送にも In-Reply-To を付ける (清掃の外注先へ転送したもの)
    srv.folders["Sent"][1] = sent_raw(1, to="clean@partner.example", subject="Fwd: 件名1",
                                      in_reply_to="<m1@example.com>")
    # 社内のアドレスにだけ送ったもの (相手には返信していない)
    srv.folders["Sent"][2] = sent_raw(2, to="kurosu@bics.example", subject="Re: 件名2",
                                      in_reply_to="<m2@example.com>")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    assert mail_status(tmp_path, m1)["status"] == "unhandled"
    assert mail_status(tmp_path, m2)["status"] == "unhandled"
    got = rows(tmp_path, "SELECT linked_mail_id, marked_replied FROM sent_mails ORDER BY id")
    assert [(g["linked_mail_id"], g["marked_replied"]) for g in got] == [(m1, 0), (m2, 0)]


def test_mail_imported_after_its_reply_is_marked_replied(client, users, tmp_path, monkeypatch):
    """返信を先に取り込み、届いたメールが後から入ったとき (別のアドレスを後で足した など) も返信済みになる."""
    from server import mail
    _, _, admin = users
    add_account(client, admin, **INFO)
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(1, to="yamada@example.com", subject="Re: ご相談",
                                      in_reply_to="<late@example.com>")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    assert row(tmp_path, "SELECT linked_mail_id FROM sent_mails")["linked_mail_id"] is None
    msg = EmailMessage()
    msg["From"] = "yamada@example.com"
    msg["To"] = "info@bics.example"
    msg["Subject"] = "ご相談"
    msg["Message-ID"] = "<late@example.com>"
    msg["Date"] = "Tue, 27 Oct 2026 10:00:00 +0900"
    msg["X-Priority"] = "1"
    msg.set_content("至急ご相談です")
    srv.folders["INBOX"][1] = msg.as_bytes()
    notices = []
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_fetch_enabled", 1)
        db.set_setting(conn, "notify_mail_high", 1)
        db.set_setting(conn, "mail_urgent_keywords", "至急")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.check_mail(conn, notifier=lambda s, text: notices.append(text))
    r = row(tmp_path, "SELECT * FROM mails WHERE message_id = '<late@example.com>'")
    assert r["status"] == "replied" and r["reply_source"] == "webmail"
    assert row(tmp_path, "SELECT linked_mail_id FROM sent_mails")["linked_mail_id"] == r["id"]
    # 優先度[高]の知らせは出す (Message-ID は差出人が付けられるので念のため)。返信済みにしたことを書き添える
    assert notices == ["📧 優先度[高]のメールを受信しました（Webメールからの返信で返信済みにしました）:"
                       " ご相談 (yamada@example.com)"]


# ---------- 送信済みだけ取り込むアドレス (Gmail) ----------

def test_gmail_sent_only_account(client, users, tmp_path, monkeypatch):
    _, staff, admin = users
    info = add_account(client, admin, **INFO)
    gm = add_account(client, admin, **GMAIL)
    assert gm["sent_only"] and gm["enabled"] and gm["import_sent"]
    a = row(tmp_path, "SELECT * FROM mail_accounts WHERE id = ?", (gm["id"],))
    # 前の版のコードが受信箱を読まないよう、DB では enabled=0 (使う/止めるは sent_import)
    assert (a["enabled"], a["sent_only"], a["sent_import"]) == (0, 1, 1)
    m = add_mail(tmp_path, n=1, from_addr="guest@airbnb.example", subject="鍵の受け取り")
    gmail = Server(folders={"[Gmail]/All Mail": {1: sent_raw(9)}}, sent=(GMAIL_SENT, "\\Sent"))
    gmail.folders["INBOX"][1] = sent_raw(99, to="kurosu.bics@gmail.com", subject="私用のメール")
    gmail.folders[GMAIL_SENT][4] = sent_raw(
        1, to="guest@airbnb.example", subject="Re: 鍵の受け取り", in_reply_to="<m1@example.com>",
        frm="黒須 <kurosu.bics@gmail.com>")
    info_srv = Server()
    use_servers(monkeypatch, {"imap.bics.example": info_srv, "imap.gmail.com": gmail})
    fetch(client, admin)
    # Gmail の受信箱は読まない (私用のメールは受信箱に入らない)
    assert all(folder != "INBOX" for folder, _c in gmail.searches)
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM mails")["n"] == 1
    s = row(tmp_path, "SELECT * FROM sent_mails")
    assert (s["folder"], s["source"], s["account_id"]) == ("[Gmail]/送信済みメール", "gmail", gm["id"])
    r = mail_status(tmp_path, m)
    assert (r["status"], r["replied_by"], r["reply_source"]) == ("replied", "Gmail", "gmail")
    item = sent_list(client, staff)["items"][0]
    assert (item["source_label"], item["account_label"]) == ("Gmail", "Gmail")
    # 受信の様子: 送信済みだけのアドレスは送信済みの取り込みの様子を出し、受信箱の「宛先」には出さない
    st = client.get("/api/mail/status", headers=auth(staff)).json()
    acc = {x["id"]: x for x in st["accounts"]}
    assert acc[gm["id"]]["sent_only"] and acc[gm["id"]]["enabled"] and acc[gm["id"]]["last_fetch_at"]
    assert not acc[info["id"]]["sent_only"]
    info_row = next(x for x in client.get("/api/mail/accounts", headers=auth(admin)).json()["accounts"]
                    if x["id"] == gm["id"])
    assert info_row["sent_folder"] == "[Gmail]/送信済みメール" and info_row["last_sent_fetch_at"]
    assert "gmail-app-pw" not in json.dumps(info_row)
    # 止めると取り込まない
    r = client.patch(f"/api/mail/accounts/{gm['id']}", headers=auth(admin), json={"enabled": False})
    assert r.status_code == 200 and not r.json()["enabled"] and r.json()["sent_only"]
    gmail.searches.clear()
    fetch(client, admin)
    assert gmail.searches == []
    # ふつうのアドレスに戻すと受信箱も読む (送信済みも取り込む)
    r = client.patch(f"/api/mail/accounts/{gm['id']}", headers=auth(admin),
                     json={"enabled": True, "sent_only": False})
    assert r.json()["enabled"] and not r.json()["sent_only"] and r.json()["import_sent"]
    a = row(tmp_path, "SELECT * FROM mail_accounts WHERE id = ?", (gm["id"],))
    assert (a["enabled"], a["sent_only"], a["sent_import"]) == (1, 0, 1)
    assert a["sent_state_last_uid"] == 0   # 取り込みの続きの位置は消す (二重には入らない)
    fetch(client, admin)
    assert any(folder == "INBOX" for folder, _c in gmail.searches)
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM sent_mails")["n"] == 1


def test_sent_only_account_alone_can_be_fetched(client, users, tmp_path, monkeypatch):
    from server import db, mail
    _, _, admin = users
    gm = add_account(client, admin, **GMAIL)
    gmail = Server(sent=(GMAIL_SENT, "\\Sent"))
    gmail.folders[GMAIL_SENT][1] = sent_raw(1, to="a@b.example", subject="こんにちは")
    use_servers(monkeypatch, {"imap.gmail.com": gmail})
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.receiving_configured(conn)
        # 仕事の相手 (アドレス帳の人) へのメールなので取り込む
        conn.execute("INSERT INTO address_book (name, email, created_at, updated_at)"
                     " VALUES ('', 'A@b.example', '2026-10-01', '2026-10-01')")
    fetch(client, admin)
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM sent_mails")["n"] == 1
    # 失敗したら「今すぐ受信」でも分かる (このアドレスの受信の結果)
    gmail.fail = OSError("down")
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 502 and "受信に失敗しました" in r.json()["detail"]
    st = client.get("/api/mail/status", headers=auth(admin)).json()
    assert st["error_count"] == 1 and st["last_error_account"] == gm["label"]


def test_import_sent_off_skips_the_sent_folder(client, users, tmp_path, monkeypatch):
    _, _, admin = users
    acc = add_account(client, admin, **INFO, import_sent=False)
    assert acc["import_sent"] is False and acc["enabled"]
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(1)
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    assert [f for f, _c in srv.searches] == ["INBOX"]
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM sent_mails")["n"] == 0


# ---------- 失敗はほかに広がらない ----------

def test_sent_folder_failure_is_recorded_per_account_and_isolated(client, users, tmp_path,
                                                                  monkeypatch):
    _, _, admin = users
    info = add_account(client, admin, **INFO)
    other = add_account(client, admin, from_addr="kurosu@bics2.example",
                        imap_host="imap.bics2.example", imap_user="kurosu@bics2.example",
                        imap_pass="pw2", label="黒須")
    broken = Server(sent=None)              # 送信済みフォルダが見つからない
    broken.folders["INBOX"][1] = sent_raw(50, frm="guest@example.com", to="info@bics.example",
                                          subject="受信箱のメール")
    ok = Server()
    ok.folders["Sent"][1] = sent_raw(1, to="x@y.example", subject="送ったメール")
    use_servers(monkeypatch, {"imap.bics.example": broken, "imap.bics2.example": ok})
    out = fetch(client, admin)
    assert out == {"fetched": 1, "errors": []}     # 受信箱の受信は失敗していない
    a = {r["id"]: r for r in client.get("/api/mail/accounts", headers=auth(admin)).json()["accounts"]}
    assert "送信済みフォルダが見つかりませんでした" in a[info["id"]]["last_sent_error"]
    assert a[info["id"]]["last_error"] == "" and a[info["id"]]["last_fetch_at"]
    assert a[other["id"]]["last_sent_error"] == "" and a[other["id"]]["last_sent_fetch_at"]
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM sent_mails")["n"] == 1
    st = client.get("/api/mail/status", headers=auth(admin)).json()
    assert st["error_count"] == 0       # 受信箱の上の注意には出さない (設定の一覧に出す)
    # 送信済みフォルダの名前を直したら、記録した失敗は消える
    r = client.patch(f"/api/mail/accounts/{info['id']}", headers=auth(admin),
                     json={"imap_sent_folder": "INBOX.Sent"})
    assert r.status_code == 200 and r.json()["last_sent_error"] == ""


def test_refused_reply_copy_does_not_mark_the_mail_replied(client, users, tmp_path, monkeypatch):
    from server import notify
    _, _, admin = users
    add_account(client, admin, **INFO)
    m = add_mail(tmp_path, n=1)
    srv = Server()
    use_servers(monkeypatch, {"imap.bics.example": srv})
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, frm, rcpts, raw: {"taro@example.com": (550, b"no such user")})
    r = client.post(f"/api/mail/{m}/send", headers=auth(admin),
                    json={"body": "返信", "cc": ["boss@other.example"]})
    assert r.status_code == 502
    assert len(srv.folders["Sent"]) == 1      # CC の人に届いた分の控え
    assert mail_status(tmp_path, m)["status"] == "unhandled"
    fetch(client, admin)
    assert mail_status(tmp_path, m)["status"] == "unhandled"
    s = rows(tmp_path, "SELECT * FROM sent_mails")
    assert len(s) == 1 and s[0]["source"] == "app" and "届きませんでした" in s[0]["note"]
    item = sent_list(client, admin)["items"][0]
    assert item["source_label"] == "この画面" and "届きませんでした" in item["note"]


# ---------- 見られる人・変えられる人 ----------

def test_permissions(client, users, tmp_path):
    worker, staff, admin = users
    acc = add_account(client, admin, **INFO)
    for path in ("/api/mail/sent", "/api/mail/sent/i1"):
        assert client.get(path, headers=auth(worker)).status_code == 403
        assert client.get(path).status_code == 401
    assert client.get("/api/mail/sent", headers=auth(staff)).status_code == 200
    # アドレスの設定を変えられるのは管理者だけ
    r = client.patch(f"/api/mail/accounts/{acc['id']}", headers=auth(staff),
                     json={"sent_only": True})
    assert r.status_code == 403
    r = client.post("/api/mail/accounts", headers=auth(staff), json={**GMAIL})
    assert r.status_code == 403
    assert client.get("/api/mail/sent?account=abc", headers=auth(staff)).status_code == 400
    assert client.get("/api/mail/sent?limit=0", headers=auth(staff)).status_code == 400


# ---------- 移行・速さ・前の版 ----------

def test_migration_is_idempotent_and_list_is_fast(tmp_path):
    from server import db, sent_box
    path = tmp_path / "zatsumu.db"
    conn = db.connect(path)
    now = "2026-10-01T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO mails (message_id, from_addr, subject, body, received_at, fetched_at,"
        " status, replied_at, reply_body, reply_sent_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(f"<m{i}@x>", f"u{i % 900}@co{i % 300}.example.jp", f"件名{i}", "本文" * 300, now, now,
          "replied" if i % 2 else "unhandled",
          f"2026-10-{1 + i % 28:02d}T00:{i % 60:02d}:00+00:00" if i % 2 else None,
          "返信" * 100 if i % 2 else "", f"u{i % 900}@co{i % 300}.example.jp")
         for i in range(50000)])
    conn.executemany(
        "INSERT INTO sent_mails (message_id, to_addr, to_key, subject, body, sent_at, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(f"<s{i}@x>", f"v{i}@x.example", f"v{i}@x.example", "件名", "本文" * 300,
          f"2026-10-{1 + i % 28:02d}T01:00:00+00:00", now) for i in range(20000)])
    conn.commit()
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT m.id FROM mails m WHERE " + sent_box._APP_WHERE
        + " ORDER BY m.replied_at DESC LIMIT 51"))
    assert "idx_mails_app_sent" in plan and "TEMP B-TREE" not in plan, plan
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT s.id FROM sent_mails s ORDER BY s.sent_at DESC LIMIT 51"))
    assert "idx_sent_mails_sent_at" in plan, plan
    t = time.time()
    out = sent_box.list_sent(conn, 0, 50)
    assert out["total"] == 45000 and len(out["items"]) == 50 and time.time() - t < 2
    t = time.time()
    sent_box.list_sent(conn, 2000, 50)
    sent_box.newest_mark(conn)
    assert time.time() - t < 2
    conn.close()
    conn = db.connect(path)
    try:
        assert conn.total_changes == 0
        cols = {r[1] for r in conn.execute("PRAGMA table_info(mail_accounts)")}
        assert {"sent_only", "sent_import", "sent_state_last_uid", "sent_fetch_error"} <= cols
    finally:
        conn.close()


def test_purge_removes_old_sent_mails_and_their_files(client, users, tmp_path, monkeypatch):
    from server import db, mail
    _, _, admin = users
    add_account(client, admin, **INFO)
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(1, attach=("a.pdf", b"%PDF"))
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    sid = row(tmp_path, "SELECT id FROM sent_mails")["id"]
    folder = tmp_path / "mail_attachments" / "sent" / str(sid)
    assert folder.is_dir()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE sent_mails SET fetched_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        mail.purge_old_mails(conn, 30, tmp_path / "mail_attachments")
        assert conn.execute("SELECT COUNT(*) FROM sent_mails").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sent_mail_attachments").fetchone()[0] == 0
    assert not folder.exists()


def _old_code_dir(tmp_path, rev=PREVIOUS):
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", rev + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip("前の版のコミットがありません (浅い clone)")
    out = tmp_path / "old_code"
    out.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", rev, "server"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(out)], input=archive, check=True)
    return out


def test_previous_version_runs_on_new_db(tmp_path, monkeypatch):
    """前の版のコードが新しい DB で一覧・返信・受信でき、送信済みだけのアドレス (Gmail) の受信箱は読まない."""
    old_dir = _old_code_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(data))
    import importlib
    from server import app as app_module, db
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    with db.get_db(data / "zatsumu.db") as conn:
        admin = db.create_user(conn, "黒須", is_admin=True)
        db.set_setting(conn, "smtp_host", "smtp.example.com")
        db.set_setting(conn, "mail_auto_draft", 0)
    for fields in (INFO, GMAIL):
        assert c.post("/api/mail/accounts", headers=auth(admin), json=fields).status_code == 200
    m = add_mail(data, n=1)
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(1, in_reply_to="<m1@example.com>")
    use_servers(monkeypatch, {"imap.bics.example": srv, "imap.gmail.com": Server()})
    assert c.post("/api/mail/fetch", headers=auth(admin)).status_code == 200
    assert mail_status(data, m)["status"] == "replied"
    add_mail(data, n=2)
    env = {**os.environ, "ZATSUMU_DATA_DIR": str(data)}
    env.pop("ZATSUMU_DEMO", None)
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(old_dir)!r})
        from email.message import EmailMessage
        from fastapi.testclient import TestClient
        from server import app as app_module, mail, notify, db
        assert app_module.__file__.startswith({str(old_dir)!r})
        assert "sent_only" not in open(mail.__file__, encoding="utf-8").read()
        sent = []
        notify.smtp_send = lambda s, sender, rcpts, raw: sent.append(list(rcpts)) or {{}}
        hosts = []
        class FakeIMAP:
            def __init__(self, host): self.host = host
            def select(self, folder, readonly=False): return ("OK", [b"1"])
            def response(self, key): return ("UIDVALIDITY", [b"1"])
            def uid(self, cmd, *args):
                if cmd == "search": return ("OK", [b"1"])
                m = EmailMessage()
                m["From"] = "new@guest.example"
                m["To"] = "info@bics.example"
                m["Subject"] = "前の版で受信"
                m["Message-ID"] = "<oldfetch-" + self.host + "@x>"
                m.set_content("本文")
                return ("OK", [(b"h", m.as_bytes())])
            def list(self, *a): return ("OK", [b'(\\\\Sent) "/" "Sent"'])
            def append(self, *a): return ("OK", [b""])
            def logout(self): pass
        def connect(s):
            hosts.append(s["imap_host"])
            return FakeIMAP(s["imap_host"])
        mail._imap_connect = connect
        c = TestClient(app_module.app)
        h = {{"Authorization": "Bearer {admin['token']}"}}
        rows = c.get("/api/mail?status=", headers=h).json()
        d = c.get("/api/mail/1", headers=h).json()
        r = c.post("/api/mail/2/send", headers=h, json={{"body": "前の版の返信"}})
        f = c.post("/api/mail/fetch", headers=h)
        accs = c.get("/api/mail/accounts", headers=h).json()["accounts"]
        st = c.get("/api/mail/status", headers=h).json()
        print(json.dumps({{"count": len(rows), "status": d["status"], "send": r.status_code,
                          "fetch": f.status_code, "hosts": hosts, "accounts": len(accs),
                          "gmail_enabled": [a["enabled"] for a in accs if "gmail" in a["imap_host"]],
                          "unhandled": st["unhandled"]}}))
    """)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(old_dir), env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (out["count"], out["status"], out["send"], out["fetch"]) == (2, "replied", 200, 200)
    # 前の版は Gmail (送信済みだけ) の受信箱を読まない (止めているアドレスに見える)
    assert "imap.gmail.com" not in out["hosts"] and "imap.bics.example" in out["hosts"]
    assert out["gmail_enabled"] == [False]
    # 新しい版に戻っても、前の版が送った返信は送信箱の「この画面」に出て、控えは取り込まない
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    lst = c.get("/api/mail/sent", headers=auth(admin)).json()
    assert {i["kind"] for i in lst["items"]} == {"app", "imported"} and lst["total"] == 2


# ---------- 画面 ----------

def test_mail_page_has_sent_box(client):
    page = client.get("/mail").text
    for marker in ('id="nav-sent">送信箱</button>', 'id="sent-view"', 'id="sent-detail"',
                   'id="sd-body"', 'id="sent-list"', 'id="sent-more"', 'id="sent-acct"',
                   'id="acct-sent-only"', 'id="acct-import-sent"', "imap.gmail.com",
                   "アプリ パスワード", "2段階認証", "送信済みだけ取り込む（受信箱には入れない）",
                   '"#sent"', "sent_mark", 'id="m-reply-via"'):
        assert marker in page, marker
    # 外から来た文字 (宛先・件名・本文) は esc() か textContent で入れる
    for safe in ("${hl(it.subject, T)", "hl(it.first_line, T)", "${hl(toLabel(it), T)",
                 '$("sd-body").textContent', '$("sd-subject").textContent'):
        assert safe in page, safe
    for unsafe in ("${it.subject}", "${it.first_line}", "${d.body}"):
        assert unsafe not in page, unsafe
    # 本文は折り返して全部出す (中で別にスクロールさせない)
    assert "#sd-body { max-height: none; overflow: visible; }" in page


# ---------- 返信済みにするのは、相手に届いた返信だけ (レビューの指摘) ----------

def inbox_raw(msgid, frm="taro@example.com", subject="件名", date="Tue, 27 Oct 2026 10:00:00 +0900",
              in_reply_to="", references="", priority=""):
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = "info@bics.example"
    msg["Subject"] = subject
    msg["Message-ID"] = msgid
    msg["Date"] = date
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if priority:
        msg["X-Priority"] = priority
    msg.set_content("本文です")
    return msg.as_bytes()


def test_reply_only_to_a_third_party_or_colleague_does_not_mark_replied(client, users, tmp_path,
                                                                       monkeypatch):
    """Webメールで「返信」を押して、宛先を外注先・社内の人に変えて送ったものでは返信済みにしない."""
    _, _, admin = users
    add_account(client, admin, **INFO)
    m_out = add_mail(tmp_path, n=1, subject="鍵の件")
    m_in = add_mail(tmp_path, n=2, subject="ご相談")
    m_ok = add_mail(tmp_path, n=3, subject="お見積もり")
    srv = Server()
    # 外注先にだけ (件名は Re: のまま・In-Reply-To はお客様のメール)
    srv.folders["Sent"][1] = sent_raw(1, to="gaichu@sub.example", subject="Re: 鍵の件",
                                      in_reply_to="<m1@example.com>", body="確認お願いします")
    # 同じドメインの社内の人にだけ (受信するアドレスではない)
    srv.folders["Sent"][2] = sent_raw(2, to="tanaka@bics.example", subject="Re: ご相談",
                                      in_reply_to="<m2@example.com>", body="この件どうしましょう？")
    # お客様に CC で入れて、外注先に送ったもの (お客様に届いているので返信済み)
    srv.folders["Sent"][3] = sent_raw(3, to="gaichu@sub.example", cc="Taro@Example.com",
                                      subject="Re: お見積もり", in_reply_to="<m3@example.com>")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    got = {g["message_id"]: g for g in rows(tmp_path, "SELECT * FROM sent_mails")}
    # つなぐ (送信箱と返信画面の「元のメール」には出る) が、返信済みにはしない
    assert got["<w1@webmail.example>"]["linked_mail_id"] == m_out
    assert got["<w2@webmail.example>"]["linked_mail_id"] == m_in
    assert got["<w1@webmail.example>"]["marked_replied"] == 0
    assert got["<w2@webmail.example>"]["marked_replied"] == 0
    for mid in (m_out, m_in):
        r = mail_status(tmp_path, mid)
        assert (r["status"], r["reply_source"], r["reply_sent_id"]) == ("unhandled", "", None), mid
    r = mail_status(tmp_path, m_ok)
    assert (r["status"], r["reply_source"]) == ("replied", "webmail")
    assert got["<w3@webmail.example>"]["marked_replied"] == 1


def test_late_mail_is_marked_only_by_a_direct_reply_to_its_sender(client, users, tmp_path,
                                                                 monkeypatch):
    """後から届いたメールを返信済みにするのは、そのメールへの直接の返信 (In-Reply-To) で、
    相手に送っていて、届いた後に送ったものだけ。Message-ID は差出人が付けられるので、
    こちらが送ったメールと同じ Message-ID のメールや、References に入っているだけのものではしない."""
    from server import db, mail
    _, _, admin = users
    add_account(client, admin, **INFO)
    srv = Server()
    # S1: Webメールから新しく送ったメール。S2: その続き (In-Reply-To・References は S1)
    srv.folders["Sent"][1] = sent_raw(1, to="yamada@example.com", subject="ご案内",
                                      date="Mon, 26 Oct 2026 10:00:00 +0900")
    srv.folders["Sent"][2] = sent_raw(2, to="yamada@example.com", subject="Re: ご案内",
                                      in_reply_to="<w1@webmail.example>",
                                      references="<w1@webmail.example> <old@example.com>",
                                      date="Mon, 26 Oct 2026 11:00:00 +0900")
    # S3: <q@example.com> への返信 (宛先は相手)。S4: <early@example.com> への返信 (届く前の時刻)
    srv.folders["Sent"][3] = sent_raw(3, to="yamada@example.com", subject="Re: 質問",
                                      in_reply_to="<q@example.com>",
                                      date="Wed, 28 Oct 2026 10:00:00 +0900")
    srv.folders["Sent"][4] = sent_raw(4, to="yamada@example.com", subject="Re: 早い",
                                      in_reply_to="<early@example.com>",
                                      date="Sun, 25 Oct 2026 10:00:00 +0900")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    assert all(g["linked_mail_id"] is None for g in rows(tmp_path, "SELECT * FROM sent_mails"))
    # 外の人が、こちらの S1 と同じ Message-ID のメールを送ってくる (優先度[高])
    srv.folders["INBOX"][1] = inbox_raw("<w1@webmail.example>", frm="yamada@example.com",
                                        subject="至急 ご案内", priority="1")
    # References に入っているだけ (S2 の References の <old@…>)
    srv.folders["INBOX"][2] = inbox_raw("<old@example.com>", frm="yamada@example.com",
                                        subject="前の件")
    # S3 の返信の元だが、差出人が別の人 (S3 の宛先ではない)
    srv.folders["INBOX"][3] = inbox_raw("<q@example.com>", frm="other@else.example",
                                        subject="質問")
    # S4 の返信の元だが、S4 を送った後に届いた (日付の上で)
    srv.folders["INBOX"][4] = inbox_raw("<early@example.com>", frm="yamada@example.com",
                                        subject="早い", date="Tue, 27 Oct 2026 10:00:00 +0900")
    notices = []
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_fetch_enabled", 1)
        db.set_setting(conn, "notify_mail_high", 1)
        db.set_setting(conn, "mail_urgent_keywords", "至急")
        mail.check_mail(conn, notifier=lambda s, text: notices.append(text))
    by_msgid = {r["message_id"]: r for r in rows(tmp_path, "SELECT * FROM mails")}
    assert len(by_msgid) == 4
    assert all(r["status"] == "unhandled" and r["reply_source"] == "" for r in by_msgid.values())
    got = {g["message_id"]: g for g in rows(tmp_path, "SELECT * FROM sent_mails")}
    assert got["<w1@webmail.example>"]["linked_mail_id"] is None
    assert got["<w2@webmail.example>"]["linked_mail_id"] == by_msgid["<old@example.com>"]["id"]
    assert got["<w3@webmail.example>"]["linked_mail_id"] == by_msgid["<q@example.com>"]["id"]
    assert got["<w4@webmail.example>"]["linked_mail_id"] == by_msgid["<early@example.com>"]["id"]
    assert all(g["marked_replied"] == 0 for g in got.values())
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'mail_auto_replied'")["n"] == 0
    assert len(notices) == 1 and "返信済み" not in notices[0]


def test_reply_to_a_follow_up_not_yet_fetched_marks_that_mail_later(client, users, tmp_path,
                                                                   monkeypatch):
    """お客様の続きのメール B を取り込む前に、B への返信を取り込んだとき: 前のメール A は返信済みに
    せず (A にはつなぐだけ)、B が届いたら B につなぎ直して返信済みにする (二重の返信を防ぐ)."""
    _, _, admin = users
    add_account(client, admin, **INFO)
    a = add_mail(tmp_path, n=1, subject="ご相談")
    srv = Server()
    srv.folders["Sent"][1] = sent_raw(1, subject="Re: ご相談", in_reply_to="<b@example.com>",
                                      references="<m1@example.com> <b@example.com>")
    use_servers(monkeypatch, {"imap.bics.example": srv})
    fetch(client, admin)
    s = row(tmp_path, "SELECT * FROM sent_mails")
    assert (s["linked_mail_id"], s["marked_replied"]) == (a, 0)
    assert mail_status(tmp_path, a)["status"] == "unhandled"
    # 件名だけでも A に決まりそうだが、In-Reply-To の元 (B) があるので件名では返信済みにしない
    srv.folders["INBOX"][1] = inbox_raw("<b@example.com>", subject="Re: ご相談",
                                        in_reply_to="<m1@example.com>",
                                        references="<m1@example.com>")
    fetch(client, admin)
    b = row(tmp_path, "SELECT * FROM mails WHERE message_id = '<b@example.com>'")
    assert (b["status"], b["reply_source"], b["reply_sent_id"]) == ("replied", "webmail", s["id"])
    s = row(tmp_path, "SELECT * FROM sent_mails")
    assert (s["linked_mail_id"], s["marked_replied"]) == (b["id"], 1)
    assert mail_status(tmp_path, a)["status"] == "unhandled"


def test_sent_only_account_imports_only_business_mail(client, users, tmp_path, monkeypatch):
    """個人の Gmail (送信済みだけ取り込む) は、仕事のメールだけ送信箱に入れる (私用のメールと
    その添付は、メール担当者が見られる送信箱・検索に入れない)."""
    _, staff, admin = users
    add_account(client, admin, **INFO)
    gm = add_account(client, admin, **GMAIL)
    add_mail(tmp_path, n=1, from_addr="guest@airbnb.example", subject="鍵の受け取り")
    add_mail(tmp_path, n=2, from_addr="client@abc.example", subject="定例")
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("INSERT INTO address_book (name, email, created_at, updated_at)"
                     " VALUES ('清掃', 'seisou@clean.example', '2026-10-01', '2026-10-01')")
        db.set_setting(conn, "mail_fetch_days", 30)
    gmail = Server(sent=(GMAIL_SENT, "\\Sent"))
    box = gmail.folders[GMAIL_SENT]
    frm = "黒須 <kurosu.bics@gmail.com>"
    box[1] = sent_raw(1, to="guest@airbnb.example", subject="Re: 鍵の受け取り",
                      in_reply_to="<m1@example.com>", frm=frm)                  # 届いたメールへの返信
    box[2] = sent_raw(2, to="Client@ABC.example", subject="来週の予定", frm=frm)  # 前にメールをくれた相手
    box[3] = sent_raw(3, to="家族 <mama@family.example>", subject="週末", frm=frm,
                      attach=("写真.pdf", b"%PDF private"))                      # 私用
    box[4] = sent_raw(4, to="tomo@friend.example", cc="seisou@clean.example", subject="日程",
                      frm=frm)                                                  # アドレス帳の人に CC
    box[5] = sent_raw(5, to="info@bics.example", subject="メモ", frm=frm)       # 自分たちだけ
    box[6] = sent_raw(6, to="shop@store.example", subject="注文", frm=frm)      # 私用
    srv = Server()
    use_servers(monkeypatch, {"imap.bics.example": srv, "imap.gmail.com": gmail})
    fetch(client, admin)
    got = {g["message_id"] for g in rows(tmp_path, "SELECT message_id FROM sent_mails")}
    assert got == {"<w1@webmail.example>", "<w2@webmail.example>", "<w4@webmail.example>"}
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM sent_mail_attachments")["n"] == 0
    assert not list((tmp_path / "mail_attachments").glob("sent/*/*"))
    # 取り込まなかったものも、次の回に読み直さない
    a = row(tmp_path, "SELECT * FROM mail_accounts WHERE id = ?", (gm["id"],))
    assert a["sent_state_last_uid"] == 6
    lst = sent_list(client, staff, q="週末")
    assert lst["total"] == 0
    # ふつうのアドレス (info@ の送信済みフォルダ) は全部取り込む (仕事のメールボックス)
    srv.folders["Sent"][1] = sent_raw(11, to="new@someone.example", subject="ご挨拶")
    fetch(client, admin)
    assert row(tmp_path, "SELECT COUNT(*) AS n FROM sent_mails WHERE message_id = '<w11@webmail.example>'")["n"] == 1
