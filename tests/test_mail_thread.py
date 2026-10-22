"""同じ相手とのやり取り (返信画面の「この相手とのやり取り」・一覧の「やり取り N件」) のテスト.

- 同じアドレス (大文字・小文字は区別しない) のメールをまとめる・このメール自身は入れない
- Message-ID・In-Reply-To・References でつながるメール (別のアドレスからの返事など) もまとめる
- 新しい順・件数の上限・こちらが送った返信 (本文・時刻・送った人・CC) も付ける
- 送った返信の Message-ID を残し、その返信への返事とつなげる
- 前からあるメール (References だけ残っている) のつながりを起動時に作る
- メール全部を読まずに索引で探す

ネットワークには出ない (IMAP・SMTP は偽物に差し替える)。
"""
import email
import sqlite3
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth
from tests.test_mail_attachments import FakeIMAP


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
    monkeypatch.delenv("ZATSUMU_DEMO", raising=False)
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


def _mail(tmp_path, n, refs="", in_reply_to="", **kw):
    """受信メールを1通入れ、届いたときと同じように返事の元 (mail_refs) も覚える."""
    from server import db, mail
    mid = _add_mail(tmp_path, n=n, references_hdr=refs, **kw)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_refs(conn, mid, in_reply_to, refs)
    return mid


def _set(tmp_path, mid, **cols):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        for k, v in cols.items():
            conn.execute(f"UPDATE mails SET {k} = ? WHERE id = ?", (v, mid))


def _detail(client, admin, mid):
    r = client.get(f"/api/mail/{mid}", headers=auth(admin))
    assert r.status_code == 200, r.text
    return r.json()


def _thread_ids(client, admin, mid):
    return [t["id"] for t in _detail(client, admin, mid)["thread"]]


# ---------- 同じアドレス ----------

def test_thread_groups_same_address_newest_first(client, users, tmp_path):
    _, admin = users
    a1 = _mail(tmp_path, 1, from_addr="guest@example.com")
    a2 = _mail(tmp_path, 2, from_addr="Guest@Example.COM")     # 大文字・小文字の違い
    a3 = _mail(tmp_path, 3, from_addr="guest@example.com")
    other = _mail(tmp_path, 4, from_addr="someone@example.com")
    d = _detail(client, admin, a2)
    # このメール自身は入れず、前後のメールを新しい順に
    assert [t["id"] for t in d["thread"]] == [a3, a1]
    assert d["thread_total"] == 2
    assert _thread_ids(client, admin, a1) == [a3, a2]
    assert _thread_ids(client, admin, other) == []
    t = d["thread"][0]
    assert t["subject"] == "件名3" and t["status"] == "unhandled"
    assert t["from_addr"] == "guest@example.com" and t["snippet"] == "本文"
    assert t["received_at"].startswith("2026-08-13")


def test_list_shows_count_of_other_mails_from_same_address(client, users, tmp_path):
    _, admin = users
    a1 = _mail(tmp_path, 1, from_addr="guest@example.com")
    a2 = _mail(tmp_path, 2, from_addr="GUEST@example.com", status="replied")
    a3 = _mail(tmp_path, 3, from_addr="guest@example.com", status="archived")
    one = _mail(tmp_path, 4, from_addr="once@example.com")
    e1 = _mail(tmp_path, 5, from_addr="")
    e2 = _mail(tmp_path, 6, from_addr="")
    rows = {m["id"]: m for m in client.get("/api/mail", headers=auth(admin)).json()}
    # 状態に関係なく、同じ相手から届いたほかのメールを数える
    assert rows[a1]["thread_count"] == 2
    assert {rows[a2]["thread_count"], rows[a3]["thread_count"]} == {2}
    assert rows[one]["thread_count"] == 0
    # 差出人が分からないメールどうしは、同じ相手とみなさない
    assert rows[e1]["thread_count"] == rows[e2]["thread_count"] == 0
    assert _thread_ids(client, admin, e1) == []
    assert _detail(client, admin, a1)["thread_count"] == 2


def test_thread_is_capped_to_newest(client, users, tmp_path):
    from server import mail
    _, admin = users
    ids = [_mail(tmp_path, n, from_addr="many@example.com",
                 received_at=f"2026-08-01T00:{n:02d}:00+00:00")
           for n in range(1, 36)]
    # 一番古い1通を開くと、新しい方から30件 (全部の件数は別に返す)
    d = _detail(client, admin, ids[0])
    assert len(d["thread"]) == mail.THREAD_MAX == 30
    assert d["thread_total"] == 34
    assert [t["id"] for t in d["thread"]] == ids[:0:-1][:30]
    # 一番新しい1通を開くと、自分自身は入らず、その前から30件 (古い4通は外れる)
    d = _detail(client, admin, ids[-1])
    assert [t["id"] for t in d["thread"]] == ids[-2::-1][:30]
    assert ids[-1] not in [t["id"] for t in d["thread"]] and d["thread_total"] == 34


def test_snippet_is_short_and_on_one_line(client, users, tmp_path):
    from server import mail
    _, admin = users
    a1 = _mail(tmp_path, 1, body="山田様\n\n  いつも\tお世話に\r\nなっております。" + "あ" * 300)
    a2 = _mail(tmp_path, 2)
    (t,) = _detail(client, admin, a2)["thread"]
    assert t["id"] == a1
    assert t["snippet"].startswith("山田様 いつも お世話に なっております。")
    assert t["snippet"].endswith("…")
    assert len(t["snippet"]) == mail.THREAD_SNIPPET_CHARS + 1


def test_thread_includes_the_reply_we_sent(client, users, tmp_path):
    _, admin = users
    a1 = _mail(tmp_path, 1, status="replied")
    _set(tmp_path, a1, reply_body="山田様\nご連絡ありがとうございます。",
         replied_at="2026-08-11T03:00:00+00:00", replied_by="boss",
         reply_cc="sato@example.com, keiri@example.com", reply_bcc="secret@example.com")
    a2 = _mail(tmp_path, 2)
    d = _detail(client, admin, a2)
    (t,) = d["thread"]
    assert t["status"] == "replied"
    assert t["reply_body"] == "山田様\nご連絡ありがとうございます。"   # 返信は全文
    assert t["replied_at"] == "2026-08-11T03:00:00+00:00" and t["replied_by"] == "boss"
    assert t["reply_cc"] == ["sato@example.com", "keiri@example.com"]
    assert "reply_bcc" not in t       # BCC はやり取りの一覧には出さない
    # 返信していないメールは空
    (t,) = _detail(client, admin, a1)["thread"]
    assert (t["reply_body"], t["replied_at"], t["replied_by"], t["reply_cc"]) == (
        "", None, "", [])


def test_detail_without_thread(client, users, tmp_path):
    """やり取りの1通の全文を読むとき (thread=false) は、やり取りを探さない."""
    _, admin = users
    _mail(tmp_path, 1)
    a2 = _mail(tmp_path, 2, body="全文です")
    d = client.get(f"/api/mail/{a2}?thread=false", headers=auth(admin)).json()
    assert d["thread"] == [] and d["thread_total"] == 0 and d["body"] == "全文です"
    worker, _ = users
    assert client.get(f"/api/mail/{a2}", headers=auth(worker)).status_code == 403


# ---------- ヘッダでつながるメール ----------

def test_parse_message_ids():
    from server import mail
    assert mail.message_ids('<a@x> (Taro\'s message) <b@y>\r\n <a@x> junk') == ["<a@x>", "<b@y>"]
    assert mail.message_ids("") == [] and mail.message_ids(None) == []
    many = " ".join(f"<r{i}@x>" for i in range(50))
    refs = mail.thread_refs("<p@x>", many)
    # In-Reply-To と、References の新しい方 (末尾) から決まった数だけ
    assert refs[0] == "<p@x>" and refs[-1] == "<r49@x>"
    assert len(refs) == 1 + mail.REFS_PER_MAIL and "<r0@x>" not in refs


def test_thread_links_by_headers_across_addresses(client, users, tmp_path):
    _, admin = users
    # A: お客様の最初のメール。こちらから返信済み (返信の Message-ID が残っている)
    a = _mail(tmp_path, 1, message_id="<a@cust.example>", from_addr="cust@example.com")
    _set(tmp_path, a, status="replied", reply_body="返信です",
         replied_at="2026-08-11T05:00:00+00:00", reply_message_id="<r1@bics.example>")
    # B: 別のアドレス (同じ会社の人) から、A を References で指す返事
    b = _mail(tmp_path, 2, message_id="<b@cust.example>", from_addr="boss@cust.example",
              refs="<a@cust.example>")
    # C: スマホのアドレスから、こちらの返信にだけ In-Reply-To で返事 (References なし)
    c = _mail(tmp_path, 3, message_id="<c@phone.example>", from_addr="cust@phone.example",
              in_reply_to="<r1@bics.example>")
    # D: ここに無いメールと B を References で指している (B への返事)
    d = _mail(tmp_path, 4, message_id="<d@other.example>", from_addr="x@other.example",
              refs="<zz@unknown.example> <b@cust.example>")
    # E: C への返事 (A から見ると2段先)
    e = _mail(tmp_path, 5, message_id="<e@phone.example>", from_addr="y@else.example",
              in_reply_to="<c@phone.example>")
    # Z: 関係の無いメール
    z = _mail(tmp_path, 6, message_id="<z@x.example>", from_addr="z@x.example",
              refs="<nothing@x.example>")

    assert sorted(_thread_ids(client, admin, a)) == sorted([b, c, d, e])
    assert a in _thread_ids(client, admin, b)          # 返事の元 (References)
    assert a in _thread_ids(client, admin, c)          # 送った返信への返事 (In-Reply-To)
    assert b in _thread_ids(client, admin, d)          # 返事の元 (間にここに無いメール)
    assert d in _thread_ids(client, admin, b)          # 返事
    assert c in _thread_ids(client, admin, e)
    assert _thread_ids(client, admin, z) == []
    # 別のアドレスのものも件数に入る。返信は付いてくる
    da = _detail(client, admin, c)
    assert da["thread_total"] == len(da["thread"])
    ta = next(t for t in da["thread"] if t["id"] == a)
    assert ta["reply_body"] == "返信です" and ta["from_addr"] == "cust@example.com"
    # 一覧の件数は同じアドレスのものだけ (どれも1通ずつ)
    rows = {m["id"]: m for m in client.get("/api/mail?status=", headers=auth(admin)).json()}
    assert all(rows[i]["thread_count"] == 0 for i in (a, b, c, d, e, z))


def test_replies_to_the_same_outside_mail_are_not_mixed(client, users, tmp_path):
    """Gmail などから大勢に送ったお知らせ (ここに無い) への、別々のお客様の返事はつなげない.

    返事の元がここにあれば、同じメールへのほかの返事は2段目で見つかる。
    """
    _, admin = users
    o1 = _mail(tmp_path, 1, from_addr="owner1@example.com", in_reply_to="<notice@gmail.example>",
               refs="<notice@gmail.example>")
    o2 = _mail(tmp_path, 2, from_addr="owner2@example.com", in_reply_to="<notice@gmail.example>",
               refs="<notice@gmail.example>")
    assert _thread_ids(client, admin, o1) == [] and _thread_ids(client, admin, o2) == []
    # 元のメールがここにある (届いたメール) なら、そのメールへの返事どうしも同じやり取り
    root = _mail(tmp_path, 3, message_id="<root@cust.example>", from_addr="cust@example.com")
    r1 = _mail(tmp_path, 4, from_addr="a@cust.example", refs="<root@cust.example>")
    r2 = _mail(tmp_path, 5, from_addr="b@cust.example", in_reply_to="<root@cust.example>")
    assert sorted(_thread_ids(client, admin, r1)) == [root, r2]
    assert sorted(_thread_ids(client, admin, r2)) == [root, r1]


def test_thread_follows_headers_of_mails_from_same_address(client, users, tmp_path):
    """同じ相手の前のメールにつながる返事 (別のアドレスから) も、この相手とのやり取りに入る."""
    _, admin = users
    old = _mail(tmp_path, 1, message_id="<old@cust.example>", from_addr="cust@example.com")
    _set(tmp_path, old, reply_message_id="<r-old@bics.example>")
    boss = _mail(tmp_path, 2, from_addr="boss@cust.example", in_reply_to="<r-old@bics.example>")
    new = _mail(tmp_path, 3, from_addr="CUST@example.com")     # 新しい話 (ヘッダなし)
    d = _detail(client, admin, new)
    assert [t["id"] for t in d["thread"]] == [boss, old]
    assert d["thread_total"] == 2


def test_current_mail_is_never_in_its_own_thread(client, users, tmp_path):
    _, admin = users
    # 自分自身を指している (細工した・壊れた) メールと、同じアドレスの1通
    a = _mail(tmp_path, 1, message_id="<self@x.example>", refs="<self@x.example>",
              in_reply_to="<self@x.example>")
    b = _mail(tmp_path, 2, refs="<self@x.example>")
    assert _thread_ids(client, admin, a) == [b]
    assert _thread_ids(client, admin, b) == [a]
    assert _detail(client, admin, a)["thread_total"] == 1   # 同じアドレスとヘッダの両方でも1回


def test_header_links_are_bounded(client, users, tmp_path, monkeypatch):
    """同じメールを指しているメールがとても多くても、探すのは決まった数まで."""
    from server import mail
    monkeypatch.setattr(mail, "THREAD_LINK_MAX", 5)
    _, admin = users
    first = _mail(tmp_path, 1, message_id="<root@x.example>", from_addr="a0@x.example")
    for n in range(2, 20):
        _mail(tmp_path, n, from_addr=f"a{n}@x.example", refs="<root@x.example>")
    d = _detail(client, admin, first)
    assert len(d["thread"]) == d["thread_total"] == 5


# ---------- 受信・送信 (ヘッダを覚える) ----------

def _raw(msgid, frm, subject, in_reply_to="", references=""):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "info@bics.example"
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["Date"] = "Tue, 11 Aug 2026 10:00:00 +0900"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content("本文です")
    return m.as_bytes()


def _fetch(tmp_path, monkeypatch, mails):
    from server import db, mail
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP(mails))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com", "mail_auto_draft": 0}
        return mail.fetch_new_mail(conn, s)


def _id_of(tmp_path, msgid):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return conn.execute("SELECT id FROM mails WHERE message_id = ?", (msgid,)).fetchone()[0]


def test_reply_to_our_reply_links_back(client, users, tmp_path, monkeypatch):
    """受信→返信→その返信への返事 (別のアドレス・In-Reply-To だけ) が同じやり取りになる."""
    from server import notify
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={
        "smtp_host": "smtp.example.com", "mail_from": "info@bics.example",
        "mail_save_sent": False})
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<q1@cust.example>", "山田 <yamada@cust.example>", "清掃のご依頼",
                references="<older@cust.example>")})
    q1 = _id_of(tmp_path, "<q1@cust.example>")
    sent = []
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message: sent.append(message) or {})
    r = client.post(f"/api/mail/{q1}/send", headers=auth(admin),
                    json={"body": "承りました。", "cc": ["keiri@cust.example"]})
    assert r.status_code == 200, r.text
    reply_id = email.message_from_string(sent[0])["Message-ID"]
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        stored = conn.execute("SELECT reply_message_id FROM mails WHERE id = ?",
                              (q1,)).fetchone()[0]
        refs = {r[0] for r in conn.execute("SELECT ref FROM mail_refs WHERE mail_id = ?", (q1,))}
    assert stored == reply_id and reply_id.endswith("@bics.example>")
    assert refs == {"<older@cust.example>"}           # 届いたメールの References も覚える
    # 相手がスマホから、こちらの返信に返事 (In-Reply-To だけ)
    _fetch(tmp_path, monkeypatch, {
        2: _raw("<q2@phone.example>", "yamada-phone@phone.example", "Re: 清掃のご依頼",
                in_reply_to=reply_id)})
    q2 = _id_of(tmp_path, "<q2@phone.example>")
    (t,) = _detail(client, admin, q2)["thread"]
    assert t["id"] == q1 and t["reply_body"] == "承りました。"
    assert t["reply_cc"] == ["keiri@cust.example"] and t["replied_by"] == "boss"
    assert _thread_ids(client, admin, q1) == [q2]


def test_failed_send_leaves_no_reply_message_id(client, users, tmp_path, monkeypatch):
    from server import db, notify
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"smtp_host": "smtp.example.com"})
    mid = _mail(tmp_path, 1)

    def boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "smtp_send", boom)
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "本文"}).status_code == 502
    # 宛先に断られて未対応に戻すときも、送った返信の Message-ID は残さない
    monkeypatch.setattr(notify, "smtp_send", lambda *a, **kw: {"taro@example.com": (550, b"x")})
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "本文"}).status_code == 502
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        row = conn.execute("SELECT status, reply_message_id FROM mails WHERE id = ?",
                           (mid,)).fetchone()
    assert (row[0], row[1]) == ("unhandled", "")


def test_purge_removes_refs(tmp_path):
    from datetime import datetime, timezone

    from server import db, mail
    _mail(tmp_path, 1, refs="<p@x.example>", fetched_at="2020-01-01T00:00:00+00:00")
    keep = _mail(tmp_path, 2, refs="<p@x.example>",
                 fetched_at=datetime.now(timezone.utc).isoformat())
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30) == 1
        assert [r[0] for r in conn.execute("SELECT mail_id FROM mail_refs")] == [keep]


# ---------- 前からあるメール・前からある DB ----------

def test_fill_missing_refs_for_existing_mails(tmp_path):
    """この機能より前のメール (References の列だけある) のつながりを作る。調べ終えたものは調べない."""
    from server import db, mail
    a = _add_mail(tmp_path, n=1, message_id="<a@x.example>", from_addr="a@x.example")
    b = _add_mail(tmp_path, n=2, from_addr="b@y.example", references_hdr="<a@x.example>")
    _add_mail(tmp_path, n=3, from_addr="c@z.example")                  # ヘッダなし
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fill_missing_refs(conn) == 1
        assert not conn.in_transaction                                  # すぐ確定する
        assert mail.fill_missing_refs(conn) == 0                        # 2回目は何もしない
        row = conn.execute("SELECT * FROM mails WHERE id = ?", (a,)).fetchone()
        assert [t["id"] for t in mail.mail_thread(conn, row)["thread"]] == [b]
    # 前の版に戻っていた間に入ったメール (つながりを覚えていない) も、次の起動で作る
    c = _add_mail(tmp_path, n=4, from_addr="d@w.example", references_hdr="<a@x.example>")
    d = _mail(tmp_path, 5, from_addr="e@v.example", refs="<a@x.example>")   # 覚えている
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fill_missing_refs(conn) == 1
        row = conn.execute("SELECT * FROM mails WHERE id = ?", (a,)).fetchone()
        assert sorted(t["id"] for t in mail.mail_thread(conn, row)["thread"]) == [b, c, d]


def test_startup_fills_refs(client, users, tmp_path):
    """起動したときに、前からあるメールのつながりを作る."""
    from server import app as app_module
    _, admin = users
    a = _add_mail(tmp_path, n=1, message_id="<a@x.example>", from_addr="a@x.example")
    b = _add_mail(tmp_path, n=2, from_addr="b@y.example", references_hdr="<a@x.example>")
    assert _thread_ids(client, admin, a) == []
    with TestClient(app_module.app) as c:
        assert [t["id"] for t in c.get(f"/api/mail/{a}", headers=auth(admin)).json()[
            "thread"]] == [b]


def test_existing_db_gets_thread_columns(tmp_path):
    """前の版の DB (返信の Message-ID の列・つながりの表が無い) を開くと足され、2回開いても壊れない."""
    from server import db, mail
    path = tmp_path / "old.db"
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
    for n, (addr, refs) in enumerate([("Old@Example.com", ""), ("old@example.com", "<m0@e>")]):
        old.execute("INSERT INTO mails (message_id, from_addr, subject, received_at,"
                    " fetched_at, references_hdr) VALUES (?, ?, '前のメール', ?, ?, ?)",
                    (f"<m{n}@e>", addr, f"2026-01-0{n + 1}T00:00:00+00:00",
                     "2026-01-01T00:00:00+00:00", refs))
    old.commit()
    old.close()

    for _ in range(2):
        conn = db.connect(path)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(mails)")]
        assert cols.count("reply_message_id") == 1
        indexes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_mails_from_lower", "idx_mails_reply_msgid",
                "idx_mail_refs_ref"} <= indexes
        assert not conn.in_transaction
        conn.close()
    conn = db.connect(path)
    mail.fill_missing_refs(conn)
    row = conn.execute("SELECT * FROM mails WHERE id = 1").fetchone()
    assert row["reply_message_id"] == ""
    d = mail.mail_thread(conn, row)
    assert [t["id"] for t in d["thread"]] == [2] and d["thread_total"] == 1
    conn.close()


# ---------- 重くならないこと ----------

def test_thread_queries_use_indexes(tmp_path):
    """やり取りを探す・一覧の件数を数える問い合わせが、メール全部を読まない (索引で探す)."""
    from server import app as app_module
    from server import db, mail
    a = _mail(tmp_path, 1, message_id="<a@x.example>", from_addr="cust@example.com")
    _set(tmp_path, a, reply_message_id="<r@bics.example>")
    _mail(tmp_path, 2, from_addr="boss@cust.example", refs="<a@x.example>")
    _mail(tmp_path, 3, from_addr="CUST@example.com", in_reply_to="<r@bics.example>")
    for n in range(4, 40):
        _mail(tmp_path, n, from_addr=f"u{n}@example.com", refs=f"<t{n}@x.example>")
    conn = db.connect(tmp_path / "zatsumu.db")
    conn.execute("ANALYZE")
    statements = []
    conn.set_trace_callback(statements.append)
    row = conn.execute("SELECT * FROM mails WHERE id = ?", (a,)).fetchone()
    assert len(mail.mail_thread(conn, row)["thread"]) == 2
    conn.set_trace_callback(None)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) >= 4
    for sql in selects:
        plan = [r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        scans = [p for p in plan if p.startswith("SCAN")]
        assert not scans, (sql, plan)
    # 一覧の「やり取り N件」は1行ごとに索引で数える
    plan = [r["detail"] for r in conn.execute(
        "EXPLAIN QUERY PLAN " + app_module._MAIL_SELECT + " WHERE m.status = 'unhandled'")]
    assert any("idx_mails_from_lower" in p and p.startswith("SEARCH t") for p in plan), plan
    conn.close()


def test_list_counts_thread_without_reading_every_sender_mail_per_row(
        client, users, tmp_path):
    """同じ相手から何千通も届いていても、一覧の「やり取り N件」で遅くならない.

    前は絞り込みに合う行ごとに同じ相手のメールを数え直していた (並べ替えより先に
    数えるので、出さない行の分も。同じ相手が k 通なら k×k 回読む)。
    いまは出す行の相手ごとに1回だけ数える。読む量を SQLite の命令の数で確かめる。
    """
    from server import app as app_module
    from server import db
    _, admin = users
    n_same = 4000
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.executemany(
            "INSERT INTO mails (message_id, from_addr, subject, received_at, fetched_at,"
            " status) VALUES (?, ?, '自動のお知らせ', ?, ?, ?)",
            [(f"<auto{i}@airbnb.example>",
              "Automated@Airbnb.example" if i % 2 else "automated@airbnb.example",
              f"2026-08-01T00:00:{i % 60:02d}+00:00", "2026-08-01T00:00:00+00:00",
              "replied" if i % 3 else "unhandled") for i in range(n_same)])
    once = _mail(tmp_path, 1, from_addr="once@example.com")
    noaddr = _mail(tmp_path, 2, from_addr="")
    conn = db.connect(tmp_path / "zatsumu.db")
    try:
        steps = [0]

        def count():
            steps[0] += 1
            return 0
        conn.set_progress_handler(count, 1000)   # 1000 命令ごとに1回呼ばれる
        rows = app_module.list_mail(status=None, priority=None, category=None,
                                    assignee=None, admin=dict(admin), conn=conn)
        conn.set_progress_handler(None, 0)
    finally:
        conn.close()
    assert len(rows) == 500
    by_id = {m["id"]: m for m in rows}
    assert by_id[once]["thread_count"] == 0 and by_id[noaddr]["thread_count"] == 0
    # 大文字・小文字の違うアドレスも同じ相手として数える (このメール自身は入れない)
    assert {m["thread_count"] for m in rows if m["id"] not in (once, noaddr)} == {n_same - 1}
    # 1行ずつ数え直すと 4000×4000 回 (数千万命令) 読む。まとめて数えれば数十万命令で済む
    assert steps[0] * 1000 < 3_000_000, steps[0]
    # API からも同じ数が出る (絞り込みあり)
    api_rows = client.get("/api/mail?status=unhandled", headers=auth(admin)).json()
    assert api_rows and all(m["thread_count"] == n_same - 1
                            for m in api_rows if m["id"] not in (once, noaddr))


# ---------- 画面 ----------

def test_mail_page_has_thread_ui(client):
    page = client.get("/mail").text
    for marker in ('id="m-thread"', 'id="m-thread-title"', 'id="m-thread-list"',
                   'id="m-back"', "この相手とのやり取り（${total}件）",
                   "やり取り ${Number(m.thread_count)}件", "?thread=false",
                   "data-thr-open", "← 前のメールに戻る"):
        assert marker in page, marker
    # 返信文の生成・送信を待っている間に、やり取りの別のメールを開いたら、
    # 前のメールの結果をいま開いている返信画面に入れない
    for handler in ('$("m-gen").onclick', '$("m-send").onclick'):
        i = page.index(handler)
        body = page[i:page.index("\n};", i)]
        assert "const id = modal.id;" in body and "modal.id !== id" in body, handler
        assert "${modal.id}" not in body, handler
    # 外から来た文字 (件名・本文・差出人・返信) は esc() か textContent で出す
    for safe in ("${esc(t.subject)", "${esc(t.snippet)", "${esc(who)}",
                 "${esc(t.reply_body)", "${esc(snip(t.reply_body))}",
                 'esc(t.reply_cc.join(", "))', "el.textContent = d.body"):
        assert safe in page, safe
    assert "${t.subject" not in page and "${t.snippet" not in page
    assert "${t.reply_body" not in page and '"・" + esc(t.replied_by)' in page
    # 差出人は文字にまとめてから esc() を通す
    assert 'const who = `${t.from_name ? t.from_name + " " : ""}<${t.from_addr || "不明"}>`;' in page
