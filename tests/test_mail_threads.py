"""受信箱のスレッド (返信どうしでつながった、同じお客様との同じ話のメール) のテスト (server/mail_threads.py).

- つながり: 届いたメールへの返事 (In-Reply-To・References)・この画面から送った返信への返事・
  Webメールの返信 (送信箱) への返事。送信箱のメールが後から取り込まれても・返事が先に届いても、まとまる
- 届いたときに入れる前にスレッドを決める (入れた行をもう一度書き換えない)
- HPフォーム: 差出人も件名も同じ別々のお客様はまとめない (References を細工しても)。フォームどうしもつなげない
- 宛先の相手: 自分たちの自動返信の控え (お客様1人宛て) と、そのお客様の返事はまとまる (2人宛てはまとめない)
- まとめないもの: 外のメールへの別々の返事・一斉配信・noreply@・社内の転送・件名が同じだけのメール
- 相手が変わったら (前の版のコードが変えても) ばらして作り直す。前の版が入れたメールも後でまとめる
- rebuild は少しずつ入れたときと同じまとまりになり、2回目は何も書かない。1通だけのメールは書かない
- check は相手がそろっていないスレッドをばらす。保存日数で消えても残りはまとまったまま
- 決まり (受信するアドレスなど) が変わったら数え直す。受信の途中は何もしない
- 索引だけで探す・数える (EXPLAIN QUERY PLAN)。2万通の DB でも速い
"""
import itertools
import json
import os
import random
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWN = "info@example.com"
STAFF = "kurosu.staff@example.com"
SUBJ = "【INBICS】お問い合わせありがとうございます"


@pytest.fixture()
def conn(tmp_path):
    """受信するアドレス (info@) と、メールアドレスを登録した社内の人がいる DB (スレッドは数え始めている)."""
    from server import db, mail_threads
    c = db.connect(tmp_path / "zatsumu.db")
    c.execute("INSERT INTO mail_accounts (label, imap_host, imap_user, from_addr, created_at)"
              " VALUES ('info', 'imap.example.com', ?, ?, '2026-01-01T00:00:00+00:00')", (OWN, OWN))
    c.execute("INSERT INTO users (name, token, email) VALUES ('黒須', 'tok-k', ?)", (STAFF,))
    c.commit()
    mail_threads.process_pending(c)   # 空の DB を数え終えた (この後は少しずつ)
    yield c
    c.close()


_seq = itertools.count(1)
# 取り込んだ時刻 (保存日数で消えないように、テストを走らせた日にする)
NOW = datetime.now(timezone.utc).isoformat()


def ins(conn, frm, *, irt="", refs="", contact=None, source="from", form=0, to=OWN, cc="",
        list_mail=0, subject="件名", status="unhandled", msgid=None, reply_id="", flagged=None,
        fetched=None, commit=True):
    """届いたメールを1通入れ、届いたときと同じように返事の元 (mail_refs) も覚える (thread_id は入れない)."""
    from server import mail
    n = next(_seq)
    at = f"2026-09-{1 + n // 1440 % 28:02d}T{n // 60 % 24:02d}:{n % 60:02d}:00+00:00"
    cur = conn.execute(
        "INSERT INTO mails (message_id, from_addr, to_addr, cc_addr, subject, body, received_at,"
        " fetched_at, status, references_hdr, contact_addr, contact_source, contact_via_form,"
        " list_mail, reply_message_id, flagged_at) VALUES (?,?,?,?,?,'本文',?,?,?,?,?,?,?,?,?,?)",
        (msgid or f"<t{n}@example.net>", frm, to, cc, subject, at, fetched or NOW, status, refs,
         frm if contact is None else contact, source, form, list_mail, reply_id, flagged))
    mail.save_refs(conn, cur.lastrowid, irt, refs)
    if commit:
        conn.commit()
    return cur.lastrowid


def msgid(conn, mid):
    return conn.execute("SELECT message_id FROM mails WHERE id = ?", (mid,)).fetchone()[0]


def pend(conn):
    from server import db, mail_threads
    return mail_threads.process_pending(conn, db.get_settings(conn))


def tid(conn, mid):
    return conn.execute("SELECT thread_id FROM mails WHERE id = ?", (mid,)).fetchone()[0]


def groups(conn) -> set:
    out = {}
    for i, t in conn.execute("SELECT id, thread_id FROM mails WHERE thread_id IS NOT NULL"):
        out.setdefault(t, set()).add(i)
    return {frozenset(g) for g in out.values()}


def same(conn, *ids) -> bool:
    ts = {tid(conn, i) for i in ids}
    return len(ts) == 1 and None not in ts and ts == {min(ids)}


# ---------- つながり ----------

def test_in_reply_to_and_references_join_one_thread(conn):
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    c = ins(conn, "taro@a.example.jp", refs=f"<outside@x> {msgid(conn, b)}")   # References だけ
    pend(conn)
    assert same(conn, a, b, c)


def test_reply_to_our_reply_joins(conn):
    """A に返信 (reply_message_id) → お客様がその返信に返事 (In-Reply-To = こちらの返信だけ)."""
    a = ins(conn, "taro@a.example.jp")
    conn.execute("UPDATE mails SET reply_message_id = '<our-reply@example.com>', status = 'replied'"
                 " WHERE id = ?", (a,))
    conn.commit()
    b = ins(conn, "taro@a.example.jp", irt="<our-reply@example.com>")
    pend(conn)
    assert same(conn, a, b)


def _sent(conn, message_id, linked=None):
    cur = conn.execute(
        "INSERT INTO sent_mails (message_id, to_addr, subject, body, sent_at, fetched_at, linked_mail_id,"
        " link_kind) VALUES (?, 'taro@a.example.jp', 'Re: 件名', '本文', '2026-09-01T00:00:00+00:00',"
        " '2026-09-01T00:00:00+00:00', ?, ?)", (message_id, linked, "header" if linked else ""))
    conn.commit()
    return cur.lastrowid


def test_reply_to_webmail_reply_joins_even_if_sent_box_is_imported_later(conn):
    """Webメールの返信 S (送信箱) への返事 B。S が先でも、B の後に取り込まれても (トリガーが B を入れる)."""
    a = ins(conn, "taro@a.example.jp")
    _sent(conn, "<webmail-1@example.com>", linked=a)
    b = ins(conn, "taro@a.example.jp", irt="<webmail-1@example.com>")
    pend(conn)
    assert same(conn, a, b)
    # S が B の後に取り込まれた (取り込んだ後に、どのメールへの返信かが分かってつないだ)
    a2 = ins(conn, "jiro@b.example.jp")
    b2 = ins(conn, "jiro@b.example.jp", irt="<webmail-2@example.com>")
    pend(conn)
    assert tid(conn, a2) is None and tid(conn, b2) is None
    s2 = _sent(conn, "<webmail-2@example.com>")
    conn.execute("UPDATE sent_mails SET linked_mail_id = ?, link_kind = 'subject' WHERE id = ?", (a2, s2))
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT mail_id FROM mail_thread_todo")] == [b2]
    pend(conn)
    assert same(conn, a2, b2)
    assert conn.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0
    # 取り込んだときに、もうつながっていた (INSERT のトリガー)
    a3 = ins(conn, "saburo@c.example.jp")
    b3 = ins(conn, "saburo@c.example.jp", irt="<webmail-3@example.com>")
    pend(conn)
    _sent(conn, "<webmail-3@example.com>", linked=a3)
    pend(conn)
    assert same(conn, a3, b3)


def test_reply_id_learned_later_queues_earlier_answer(conn):
    """返事が先に届いていて、後から返信の Message-ID が分かった (Webメールの返信で返信済みにした)."""
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt="<later-reply@example.com>")
    pend(conn)
    assert tid(conn, b) is None
    conn.execute("UPDATE mails SET reply_message_id = '<later-reply@example.com>' WHERE id = ?", (a,))
    conn.commit()
    pend(conn)
    assert same(conn, a, b)


def test_child_inserted_before_parent(conn):
    b = ins(conn, "taro@a.example.jp", irt="<parent@a.example.jp>")
    c = ins(conn, "taro@a.example.jp", irt=msgid(conn, b))
    pend(conn)
    assert same(conn, b, c)
    a = ins(conn, "taro@a.example.jp", msgid="<parent@a.example.jp>")
    pend(conn)
    assert tid(conn, a) == tid(conn, b) == tid(conn, c) == b   # 印は一番小さい番号 (先に届いた返事)


def _parsed(n, frm, irt="", refs="", to=OWN, **kw):
    p = {"message_id": f"<new{n}@example.net>", "from_addr": frm, "from_name": "", "to_addr": to,
         "cc_addr": "", "subject": "Re: 件名", "body": "本文",
         "received_at": f"2026-09-20T00:{n:02d}:00+00:00", "references_hdr": refs, "in_reply_to": irt,
         "reply_to_addr": "", "contact_addr": frm, "contact_name": "", "contact_source": "from",
         "contact_via_form": 0, "contact_company": "", "list_mail": 0}
    p.update(kw)
    return (n, p, b"", 2, "", "rule", "", "", "", None)


def test_thread_is_decided_before_insert_without_rewriting_the_new_row(conn):
    """新しく届いたメールは、入れる前にスレッドを決めて INSERT に入れる (入れた行を UPDATE しない)."""
    from server import mail
    a = ins(conn, "taro@a.example.jp")
    conn.execute("CREATE TEMP TABLE upd (id INTEGER)")
    conn.execute("CREATE TEMP TRIGGER cnt AFTER UPDATE OF thread_id ON mails"
                 " BEGIN INSERT INTO upd VALUES (NEW.id); END")
    added, written = [], []
    mail._insert_mails(conn, [_parsed(1, "taro@a.example.jp", irt=msgid(conn, a)),
                              _parsed(2, "taro@a.example.jp", irt="<new1@example.net>"),
                              _parsed(3, "other@z.example.jp")],
                       [], {}, None, "2026-09-20T00:00:00+00:00", added, written,
                       thread_ctx=mail.thread_context(conn))
    conn.commit()
    ids = [x["id"] for x in added]
    assert [tid(conn, i) for i in ids] == [a, a, None]
    assert tid(conn, a) == a
    # 書き直したのは、前からあった1通だけのメール (a) だけ。新しい行は入れたときのまま
    assert [r[0] for r in conn.execute("SELECT id FROM upd")] == [a]
    # 後の process_pending も何も書き直さない
    conn.execute("DELETE FROM upd")
    pend(conn)
    assert conn.execute("SELECT COUNT(*) FROM upd").fetchone()[0] == 0


def test_new_mail_bridging_two_threads_merges_them(conn):
    from server import mail
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    c = ins(conn, "taro@a.example.jp")
    d = ins(conn, "taro@a.example.jp", irt=msgid(conn, c))
    pend(conn)
    assert same(conn, a, b) and same(conn, c, d)
    added = []
    mail._insert_mails(conn, [_parsed(9, "taro@a.example.jp", refs=f"{msgid(conn, b)} {msgid(conn, d)}")],
                       [], {}, None, "2026-09-20T00:00:00+00:00", added, [],
                       thread_ctx=mail.thread_context(conn))
    conn.commit()
    assert same(conn, a, b, c, d, added[0]["id"])


def test_fetch_threads_new_mail_end_to_end(tmp_path, monkeypatch, conn):
    """受信 (fetch_mail_locked) で届いた返事は、入れたときにはもうまとまっている."""
    from email.message import EmailMessage
    from server import db, mail
    from tests.test_mail_attachments import FakeIMAP
    a = ins(conn, "taro@a.example.jp")
    msg = EmailMessage()
    msg["From"] = "Taro <taro@a.example.jp>"
    msg["To"] = OWN
    msg["Subject"] = "Re: 件名"
    msg["Message-ID"] = "<fetched-1@a.example.jp>"
    msg["In-Reply-To"] = msgid(conn, a)
    msg["Date"] = "Sun, 20 Sep 2026 10:00:00 +0900"
    msg.set_content("お返事です")
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP({1: bytes(msg)}))
    conn.execute("UPDATE mail_accounts SET sent_import = 0")
    conn.commit()
    added = mail.fetch_mail_locked(conn, {**db.get_settings(conn), "mail_auto_draft": 0})
    assert len(added) == 1 and same(conn, a, added[0]["id"])


# ---------- HPのフォーム ----------

def _form(conn, cust, **kw):
    return ins(conn, "wordpress@example.com", contact=cust, source="body", form=1,
               subject="【HPお問い合わせ】お見積もりのご依頼", **kw)


def test_form_customers_with_same_sender_and_subject_never_merge(conn):
    f1 = _form(conn, "cust1@x.example.jp")
    f2 = _form(conn, "cust2@y.example.jp")
    b1 = ins(conn, "cust1@x.example.jp", irt=msgid(conn, f1), subject="Re: 【HPお問い合わせ】お見積もりのご依頼")
    # 別のお客様が、cust1 のフォームのメールを指す返事を送ってきた (細工した References も)
    b2 = ins(conn, "cust2@y.example.jp", irt=msgid(conn, f1), refs=f"{msgid(conn, f1)} {msgid(conn, b1)}",
             subject="Re: 【HPお問い合わせ】お見積もりのご依頼")
    pend(conn)
    assert same(conn, f1, b1)
    assert tid(conn, f2) is None and tid(conn, b2) is None
    from server import db, mail_threads
    assert mail_threads.check(conn, mail_threads.Ctx(conn, db.get_settings(conn)), repair=False) == []


def test_form_mail_without_customer_address_stays_alone(conn):
    """本文にお客様のアドレスが無いフォームのメール (自分たち宛て) は、指されてもまとめない."""
    f = _form(conn, "", to=OWN)
    b = ins(conn, "someone@x.example.jp", irt=msgid(conn, f))
    pend(conn)
    assert tid(conn, f) is None and tid(conn, b) is None


def test_two_form_mails_of_same_customer_do_not_join_directly(conn):
    f1 = _form(conn, "cust1@x.example.jp")
    f2 = _form(conn, "cust1@x.example.jp", irt=msgid(conn, f1))
    pend(conn)
    assert tid(conn, f1) is None and tid(conn, f2) is None


# ---------- 宛先の相手 (自動返信の控え) ----------

def test_auto_reply_copy_to_one_customer_joins_customer_replies(conn):
    root = ins(conn, OWN, contact=OWN, to="小川 誠 <ogawa@x.example.jp>", subject=SUBJ)
    r1 = ins(conn, "ogawa@x.example.jp", irt=msgid(conn, root), subject="RE: " + SUBJ)
    r2 = ins(conn, "ogawa@x.example.jp", irt=msgid(conn, r1), subject="RE: " + SUBJ)
    # noreply@ (自動送信) から1人宛ての控えも同じ
    root2 = ins(conn, "noreply@example.com", to="hana@y.example.jp", subject=SUBJ)
    h1 = ins(conn, "hana@y.example.jp", irt=msgid(conn, root2), subject="RE: " + SUBJ)
    pend(conn)
    assert same(conn, root, r1, r2) and same(conn, root2, h1)


def test_auto_reply_copy_to_two_customers_is_not_threaded(conn):
    root = ins(conn, OWN, contact=OWN, to="cust1@x.example.jp, cust2@y.example.jp", subject=SUBJ)
    r1 = ins(conn, "cust1@x.example.jp", irt=msgid(conn, root))
    r2 = ins(conn, "cust2@y.example.jp", irt=msgid(conn, root))
    pend(conn)
    assert tid(conn, root) is None and tid(conn, r1) is None and tid(conn, r2) is None


# ---------- まとめないもの ----------

def test_replies_to_outside_mail_from_different_customers_stay_apart(conn):
    a = ins(conn, "cust1@x.example.jp", irt="<notice@gmail.com>")
    b = ins(conn, "cust2@y.example.jp", irt="<notice@gmail.com>")
    pend(conn)
    assert tid(conn, a) is None and tid(conn, b) is None


def test_list_noreply_and_internal_chains_are_not_threaded(conn):
    l1 = ins(conn, "news@list.example.jp", list_mail=1)
    l2 = ins(conn, "news@list.example.jp", list_mail=1, irt=msgid(conn, l1))
    n1 = ins(conn, "noreply@service.example.jp")
    n2 = ins(conn, "noreply@service.example.jp", irt=msgid(conn, n1))
    # 社内の人 (登録したアドレス)・自分たちの転送 (宛先も自分たち)
    s1 = ins(conn, STAFF, subject="Fw: 件名")
    s2 = ins(conn, STAFF, irt=msgid(conn, s1), subject="Fw: 件名")
    pend(conn)
    for i in (l1, l2, n1, n2, s1, s2):
        assert tid(conn, i) is None, i


def test_same_subject_without_headers_is_not_threaded(conn):
    """決まり v1: 同じお客様の「Re:」で件名が同じでも、返信の印 (ヘッダ) が無ければまとめない."""
    a = ins(conn, "taro@a.example.jp", subject="見積もりの件")
    b = ins(conn, "taro@a.example.jp", subject="Re: 見積もりの件")
    pend(conn)
    assert tid(conn, a) is None and tid(conn, b) is None
    from server import mail_threads
    rep = mail_threads.report(conn, mail_threads.Ctx(conn))
    assert rep["v2_candidates"] == {"count": 0, "ids": []} or b in rep["v2_candidates"]["ids"]


def test_colleague_forwarding_customer_mail_is_not_joined(conn):
    a = ins(conn, "taro@a.example.jp")
    fw = ins(conn, "colleague@partner.example.jp", irt=msgid(conn, a), subject="Fw: 件名")
    pend(conn)
    assert tid(conn, a) is None and tid(conn, fw) is None


# ---------- 相手が変わったとき・前の版が入れたメール ----------

def test_contact_change_detaches_and_relinks(conn):
    from server import db, mail
    # フォームの送信元にする前: 差出人 (shop@) が相手。2通とも同じ相手なのでまとまる
    f = ins(conn, "shop@shop.example.jp")
    a = ins(conn, "shop@shop.example.jp", irt=msgid(conn, f))
    other = ins(conn, "cust1@x.example.jp", irt=msgid(conn, f))
    pend(conn)
    assert same(conn, f, a) and tid(conn, other) is None
    # 設定でフォームの送信元にした → f の相手が本文のお客様 (cust1) に変わる
    conn.execute("UPDATE mails SET body = 'メールアドレス：cust1@x.example.jp' WHERE id = ?", (f,))
    conn.commit()
    before = db.get_settings(conn)
    db.set_setting(conn, "mail_form_senders", "shop@shop.example.jp")
    mail.redecide_form_contacts(conn, db.get_settings(conn), before)
    conn.commit()
    assert tid(conn, f) is None and tid(conn, a) is None   # ばらした
    pend(conn)
    # 今は f の相手は cust1 (フォーム)。cust1 の返事とまとまる。a は本文にお客様が無いフォームの
    # メールになった (相手が分からない) ので、どこにもまとめない
    assert conn.execute("SELECT contact_addr FROM mails WHERE id = ?", (f,)).fetchone()[0] == "cust1@x.example.jp"
    assert same(conn, f, other) and tid(conn, a) is None


def test_contact_change_by_raw_sql_like_old_code(conn):
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    c = ins(conn, "taro@a.example.jp", irt=msgid(conn, b))
    pend(conn)
    assert same(conn, a, b, c)
    # 前の版のコードが、b の相手を決め直した (同じ値のままの書き換えでは何もしない)
    conn.execute("UPDATE mails SET contact_addr = contact_addr, contact_source = contact_source WHERE id = ?", (b,))
    conn.commit()
    assert same(conn, a, b, c) and conn.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0
    conn.execute("UPDATE mails SET contact_addr = 'hanako@z.example.jp' WHERE id = ?", (b,))
    conn.commit()
    assert {tid(conn, i) for i in (a, b, c)} == {None}
    assert sorted(r[0] for r in conn.execute("SELECT mail_id FROM mail_thread_todo")) == [a, b, c]
    pend(conn)
    # b は別の相手になったので、a・c ともつながらない (c は b を指すだけなので、a とも別)
    assert {tid(conn, i) for i in (a, b, c)} == {None}
    conn.execute("UPDATE mails SET contact_addr = 'TARO@a.example.jp' WHERE id = ?", (b,))   # 戻した (大文字)
    conn.commit()
    pend(conn)
    assert same(conn, a, b, c)


def test_undecided_contact_is_not_threaded_until_decided(conn):
    from server import mail
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a), source="", contact="")
    pend(conn)
    assert tid(conn, b) is None and tid(conn, a) is None
    mail.fill_missing_contacts(conn)
    pend(conn)
    assert same(conn, a, b)


def test_old_code_style_insert_is_processed_later(conn):
    """前の版のコードが入れたメール (thread_id を知らない) は、番号が覚えた所より後なので後でまとめる."""
    a = ins(conn, "taro@a.example.jp")
    pend(conn)
    cur = conn.execute(
        "INSERT INTO mails (message_id, from_addr, subject, body, received_at, fetched_at, references_hdr)"
        " VALUES ('<old-code@a.example.jp>', 'taro@a.example.jp', 'Re: 件名', '本文',"
        " '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00', ?)", (msgid(conn, a),))
    conn.commit()
    b = cur.lastrowid
    from server import mail
    mail.fill_missing_refs(conn)
    mail.fill_missing_contacts(conn)
    pend(conn)
    assert same(conn, a, b)


# ---------- 数え直し・確かめ ----------

def _scenario(conn, rnd: random.Random) -> list:
    """40通: お客様4人の返事の木・フォーム・宛先の相手・送信箱・こちらの返信。入れる順はばらばら."""
    specs = []
    custs = [f"c{i}@cust{i}.example.jp" for i in range(4)]
    for i, c in enumerate(custs):
        specs.append(dict(key=f"r{i}", frm=c))
        for j in range(5):
            specs.append(dict(key=f"r{i}-{j}", frm=c, parent=f"r{i}" if j == 0 else f"r{i}-{rnd.randrange(j)}"))
    specs.append(dict(key="f1", frm="wordpress@example.com", contact=custs[0], source="body", form=1))
    specs.append(dict(key="f2", frm="wordpress@example.com", contact=custs[1], source="body", form=1))
    specs.append(dict(key="f1a", frm=custs[0], parent="f1"))
    specs.append(dict(key="f2x", frm=custs[0], parent="f2"))            # 別のお客様のフォームへの返事
    specs.append(dict(key="auto", frm=OWN, contact=OWN, to=custs[2]))
    specs.append(dict(key="auto1", frm=custs[2], parent="auto"))
    specs.append(dict(key="our", frm=custs[3], reply_id="<our-c3@example.com>"))
    specs.append(dict(key="our1", frm=custs[3], parent_raw="<our-c3@example.com>"))
    specs.append(dict(key="web", frm=custs[1]))
    specs.append(dict(key="web1", frm=custs[1], parent_raw="<web-c1@example.com>"))
    specs.append(dict(key="lone", frm="lone@z.example.jp"))
    specs.append(dict(key="news1", frm="news@l.example.jp", list_mail=1))
    specs.append(dict(key="news2", frm="news@l.example.jp", list_mail=1, parent="news1"))
    specs.append(dict(key="x1", frm=custs[2], parent_raw="<nowhere@x>"))
    specs.append(dict(key="x2", frm=custs[3], parent_raw="<nowhere@x>"))
    rnd.shuffle(specs)
    ids = {}
    for s in specs:
        irt = f"<{s['parent']}@scenario>" if "parent" in s else s.get("parent_raw", "")
        ids[s["key"]] = ins(conn, s["frm"], irt=irt, contact=s.get("contact"), source=s.get("source", "from"),
                            form=s.get("form", 0), to=s.get("to", OWN), list_mail=s.get("list_mail", 0),
                            msgid=f"<{s['key']}@scenario>", reply_id=s.get("reply_id", ""))
        if s["key"] == "web":
            _sent(conn, "<web-c1@example.com>", linked=ids["web"])
        if rnd.random() < 0.3:
            pend(conn)
    pend(conn)
    return [{k for k, v in ids.items() if v in g} for g in groups(conn)]


def test_rebuild_equals_incremental_for_any_insertion_order(tmp_path):
    from server import db, mail_threads
    want = None
    for seed in range(20):
        c = db.connect(tmp_path / f"s{seed}.db")
        try:
            c.execute("INSERT INTO mail_accounts (label, imap_user, from_addr, created_at)"
                      " VALUES ('info', ?, ?, '2026-01-01T00:00:00+00:00')", (OWN, OWN))
            c.commit()
            mail_threads.process_pending(c)
            got = _scenario(c, random.Random(seed))
            parts = sorted(sorted(g) for g in got)
            if want is None:
                want = parts
            assert parts == want, seed
            snapshot = dict(c.execute("SELECT id, thread_id FROM mails").fetchall())
            # 全部を数え直しても同じ。2回目は何も書かない
            ctx = mail_threads.Ctx(c)
            assert mail_threads.rebuild(c, ctx) == 0
            assert dict(c.execute("SELECT id, thread_id FROM mails").fetchall()) == snapshot
            # 印を全部消してから数え直しても同じ (1通だけのメールは書かない)
            c.execute("UPDATE mails SET thread_id = NULL")
            c.commit()
            written = mail_threads.rebuild(c, ctx)
            assert written == sum(1 for v in snapshot.values() if v is not None)
            assert dict(c.execute("SELECT id, thread_id FROM mails").fetchall()) == snapshot
            assert mail_threads.rebuild(c, ctx) == 0
        finally:
            c.close()
    assert sorted(map(sorted, map(list, map(set, want)))) == want
    flat = {k for g in want for k in g}
    # まとめないもの・別のお客様のフォームへの返事は、どのスレッドにも入っていない
    assert not flat & {"lone", "news1", "news2", "x1", "x2", "f2x", "f2"}
    assert ["auto", "auto1"] in want and ["our", "our1"] in want
    assert any({"f1", "f1a", "r0"} <= set(g) for g in want) is False   # フォームとふつうのメールはつながっていない
    assert ["f1", "f1a"] in want


def test_check_detaches_thread_with_two_parties(conn):
    from server import mail_threads
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    x = ins(conn, "hanako@z.example.jp")
    pend(conn)
    conn.execute("UPDATE mails SET thread_id = ? WHERE id = ?", (a, x))   # 手で壊す
    conn.commit()
    bad = mail_threads.check(conn, mail_threads.Ctx(conn), repair=False)
    assert bad and bad[0]["key"] == a and tid(conn, x) == a       # 確かめるだけなら書かない
    bad = mail_threads.check(conn, mail_threads.Ctx(conn))
    assert [v["key"] for v in bad] == [a]
    assert {tid(conn, i) for i in (a, b, x)} == {None}
    assert sorted(r[0] for r in conn.execute("SELECT mail_id FROM mail_thread_todo")) == [a, b, x]
    log = conn.execute("SELECT admin_id, detail FROM audit_log WHERE action = 'mail_thread_repair'").fetchall()
    assert len(log) == 1 and log[0][0] == 0 and f"thread={a}" in log[0][1]
    pend(conn)
    assert same(conn, a, b) and tid(conn, x) is None


def test_purge_keeps_remaining_members_together(conn):
    from server import mail
    old = "2026-01-01T00:00:00+00:00"
    a = ins(conn, "taro@a.example.jp", fetched=old)
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    c = ins(conn, "taro@a.example.jp", irt=msgid(conn, b))
    pend(conn)
    conn.execute("INSERT INTO mail_thread_todo VALUES (?)", (a,))
    conn.commit()
    assert mail.purge_old_mails(conn, 30) == 1
    assert tid(conn, b) == tid(conn, c) == a       # 印はそのまま (番号は使い回されない)
    assert conn.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0   # 一緒に消えた


def test_rule_change_rebuilds_and_dissolves_thread(conn):
    from server import mail_threads
    a = ins(conn, "ogawa@x.example.jp")
    b = ins(conn, "ogawa@x.example.jp", irt=msgid(conn, a))
    pend(conn)
    assert same(conn, a, b)
    mail_threads.maintain(conn)
    assert same(conn, a, b)
    # そのアドレスを受信するアドレスにした (自分たちのアドレスになった) → 決まりが変わり、数え直す
    conn.execute("INSERT INTO mail_accounts (label, imap_user, from_addr, created_at)"
                 " VALUES ('ogawa', 'ogawa@x.example.jp', 'ogawa@x.example.jp', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    mail_threads.maintain(conn)
    assert tid(conn, a) is None and tid(conn, b) is None
    assert mail_threads._state(conn, mail_threads.RULE_KEY) == mail_threads.Ctx(conn).signature()


def test_maintain_skips_while_fetching(conn):
    from server import mail, mail_threads
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    assert mail._fetch_lock.acquire(blocking=False)
    try:
        mail_threads.maintain(conn)
        assert tid(conn, b) is None
    finally:
        mail._fetch_lock.release()
    mail_threads.maintain(conn)
    assert same(conn, a, b)


def test_first_run_rebuilds_everything_once(tmp_path):
    """この機能を入れた最初の回 (決まりの印が無い) は、全部を数える (前からあるメールもまとまる)."""
    from server import db, mail_threads
    c = db.connect(tmp_path / "first.db")
    try:
        a = ins(c, "taro@a.example.jp")
        b = ins(c, "taro@a.example.jp", irt=msgid(c, a))
        assert mail_threads._state(c, mail_threads.RULE_KEY) == ""
        mail_threads.process_pending(c)
        assert same(c, a, b)
        assert mail_threads._upto(c) == b and mail_threads._state(c, mail_threads.RULE_KEY)
    finally:
        c.close()


def test_failure_never_breaks_receiving(conn, monkeypatch, capsys):
    from server import mail_threads
    ins(conn, "taro@a.example.jp")
    monkeypatch.setattr(mail_threads, "link", lambda *a, **k: 1 / 0)
    assert pend(conn) == 0
    assert "スレッドをまとめられませんでした" in capsys.readouterr().out


# ---------- 木の順 ----------

def test_tree_orders_branches_oldest_first(conn):
    from server import mail_threads
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    c = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))       # a への2つ目の返事 (枝)
    d = ins(conn, "taro@a.example.jp", irt=msgid(conn, b))
    e = ins(conn, "taro@a.example.jp")                          # 指していない (最初のメールの下)
    rows = [dict(r) | {"sort_t": i} for i, r in enumerate(conn.execute(
        "SELECT id, message_id, reply_message_id FROM mails WHERE id IN (?,?,?,?,?) ORDER BY id",
        (a, b, c, d, e)))]
    walk = mail_threads.tree(conn, rows)
    assert [(m["id"], depth, parent) for m, depth, parent in walk] == [
        (a, 0, None), (b, 1, a), (d, 2, b), (c, 1, a), (e, 1, a)]


# ---------- 索引 ----------

def _plan(conn, sql, params=()):
    return " / ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_queries_use_indexes(conn, monkeypatch):
    from server import mail_threads
    a = ins(conn, "taro@a.example.jp")
    ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    pend(conn)
    agg = _plan(conn, "SELECT thread_id, COUNT(*), SUM(status IN ('unhandled','pending_approval')),"
                      " SUM(flagged_at IS NOT NULL), MAX(id), MIN(received_at) FROM mails"
                      " WHERE thread_id IS NOT NULL AND thread_id IN (1, 2) GROUP BY thread_id")
    assert "COVERING INDEX idx_mails_thread" in agg
    assert "idx_mails_thread" in _plan(conn, "SELECT m.id FROM mails m WHERE m.thread_id = ?", (a,))
    # plan() の問い合わせは、どれもメール全部を読まない
    seen = []
    real = conn.execute

    class Spy:
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("SELECT"):
                seen.append(_plan(conn, sql, params))
            return real(sql, params)
    ctx = mail_threads.Ctx(conn)
    mail_threads.plan(Spy(), ctx, dict(conn.execute(f"SELECT {mail_threads._COLS} FROM mails WHERE id = ?",
                                                    (a,)).fetchone()), [msgid(conn, a), "<x@y>"])
    assert len(seen) >= 4
    for p in seen:
        assert "SCAN mails" not in p and "SCAN x" not in p and "SCAN m" not in p, p


# ---------- 前の版のコードが、この版の DB を書き換えても ----------

PREVIOUS = "1c58752"


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


def _run_old(old_dir, data, script):
    env = {**os.environ, "ZATSUMU_DATA_DIR": str(data)}
    for k in ("ZATSUMU_DEMO", "ZATSUMU_LOGIN_VERIFY_EMAIL"):
        env.pop(k, None)
    head = f"import sys, json\nsys.path.insert(0, {str(old_dir)!r})\n"
    proc = subprocess.run([sys.executable, "-c", head + textwrap.dedent(script)],
                          cwd=str(old_dir), env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_previous_version_writes_are_picked_up(tmp_path):
    """自動更新で前の版に戻っても、この版の DB で動く (トリガーは作り直すメールを覚えるだけ)。
    前の版が入れたメール・決め直した相手・送信箱のつながり・保存日数の片付けは、この版に戻ったらまとめる."""
    from server import db, mail_threads
    old_dir = _old_code_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    path = data / "zatsumu.db"
    c = db.connect(path)
    c.execute("INSERT INTO mail_accounts (label, imap_user, from_addr, created_at)"
              " VALUES ('info', ?, ?, '2026-01-01T00:00:00+00:00')", (OWN, OWN))
    c.commit()
    mail_threads.process_pending(c)
    a = ins(c, "taro@a.example.jp", msgid="<a@x>")
    b = ins(c, "taro@a.example.jp", irt="<a@x>", msgid="<b@x>")
    gone = ins(c, "gone@g.example.jp", msgid="<g@x>", fetched="2026-01-01T00:00:00+00:00")
    gone2 = ins(c, "gone@g.example.jp", irt="<g@x>", msgid="<g2@x>", fetched="2026-01-01T00:00:00+00:00")
    pend(c)
    assert same(c, a, b) and same(c, gone, gone2)
    c.close()
    out = _run_old(old_dir, data, f"""
        from server import db, mail
        conn = db.connect({str(path)!r})
        p = {{"message_id": "<c@x>", "from_addr": "taro@a.example.jp", "from_name": "", "to_addr": {OWN!r},
              "cc_addr": "", "subject": "Re: 件名", "body": "本文", "received_at": "2026-09-25T00:00:00+00:00",
              "references_hdr": "<b@x>", "in_reply_to": "<b@x>", "reply_to_addr": "",
              "contact_addr": "taro@a.example.jp", "contact_name": "", "contact_source": "from",
              "contact_via_form": 0, "contact_company": "", "list_mail": 0}}
        added = []
        mail._insert_mails(conn, [(1, p, b"", 2, "", "rule", "", "", "", None)], [], {{}}, None,
                           {NOW!r}, added, [], None)
        # 前の版が相手を決め直した (b の相手が変わった)・返信の Message-ID を覚えた・送信箱をつないだ
        conn.execute("UPDATE mails SET contact_addr = 'other@o.example.jp' WHERE message_id = '<b@x>'")
        conn.execute("UPDATE mails SET reply_message_id = '<r@x>' WHERE message_id = '<a@x>'")
        conn.execute("INSERT INTO sent_mails (message_id, to_addr, subject, body, sent_at, fetched_at)"
                     " VALUES ('<s@x>', 't', 's', 'b', '2026-09-25T00:00:00+00:00', '2026-09-25T00:00:00+00:00')")
        conn.execute("UPDATE sent_mails SET linked_mail_id = (SELECT id FROM mails WHERE message_id = '<a@x>'),"
                     " link_kind = 'header' WHERE message_id = '<s@x>'")
        conn.commit()
        purged = mail.purge_old_mails(conn, 30)
        todo = [r[0] for r in conn.execute("SELECT mail_id FROM mail_thread_todo ORDER BY mail_id")]
        rows = [list(r) for r in conn.execute("SELECT message_id, thread_id FROM mails ORDER BY id")]
        print(json.dumps({{"added": len(added), "purged": purged, "todo": todo, "rows": rows}}))
    """)
    assert out["added"] == 1 and out["purged"] == 2
    # 相手が変わった b のスレッドはばらし、作り直すメールに入れた。前の版が入れた c は thread_id が無い
    assert out["todo"] == [a, b]
    assert out["rows"] == [["<a@x>", None], ["<b@x>", None], ["<c@x>", None]]
    c = db.connect(path)
    try:
        pend(c)
        ids = dict((m, i) for i, m in c.execute("SELECT id, message_id FROM mails"))
        # b は別の相手になったので1通だけ。c は b を指すが、相手が違うので b とはつながらない
        assert {tid(c, ids[m]) for m in ("<a@x>", "<b@x>", "<c@x>")} == {None}
        assert c.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0
    finally:
        c.close()


# ---------- 速さ (2万通) ----------

def test_performance_on_20k_mails(tmp_path):
    """2万通 (3万件ほどのつながり) の DB: 最初の数え直し・2回目 (書かない)・一覧の数え・新しいメールの決め方."""
    from server import db, mail, mail_threads
    c = db.connect(tmp_path / "big.db")
    try:
        c.execute("INSERT INTO mail_accounts (label, imap_user, from_addr, created_at)"
                  " VALUES ('info', ?, ?, '2026-01-01T00:00:00+00:00')", (OWN, OWN))
        rnd = random.Random(7)
        rows, refs = [], []
        n = 20000
        for i in range(1, n + 1):
            cust = f"cust{i % 4000}@c{i % 4000}.example.jp"
            rows.append((i, f"<p{i}@x>", cust, "本文" * 200, f"2026-0{1 + i % 9}-{1 + i % 28:02d}T00:00:00+00:00",
                         cust, "unhandled" if i % 7 == 0 else "replied"))
            if i > 4000:     # 同じお客様の前のメールへの返事 (5通ずつのスレッドになる)
                refs.append((i, f"<p{i - 4000}@x>"))
                if rnd.random() < 0.5:
                    refs.append((i, f"<outside{i}@y>"))
        c.executemany("INSERT INTO mails (id, message_id, from_addr, body, received_at, fetched_at,"
                      " contact_addr, contact_source, status) VALUES (?,?,?,?,?,?,?, 'from', ?)",
                      [(i, m, f, b, r, r, ca, st) for i, m, f, b, r, ca, st in rows])
        c.executemany("INSERT INTO mail_refs (mail_id, ref) VALUES (?, ?)", refs)
        c.commit()
        ctx = mail_threads.Ctx(c)
        t = time.perf_counter()
        written = mail_threads.rebuild(c, ctx)
        first = time.perf_counter() - t
        assert written == n
        t = time.perf_counter()
        assert mail_threads.rebuild(c, ctx) == 0
        again = time.perf_counter() - t
        keys = [r[0] for r in c.execute("SELECT DISTINCT thread_id FROM mails LIMIT 500")]
        t = time.perf_counter()
        agg = mail_threads.aggregate(c, keys)
        count = time.perf_counter() - t
        assert len(agg) == len(keys) and all(a["size"] == 5 for a in agg.values())
        t = time.perf_counter()
        for i in range(200):
            m = {"id": None, "thread_id": None, "message_id": f"<new{i}@x>", "contact_addr": "cust5@c5.example.jp",
                 "contact_source": "from", "from_addr": "cust5@c5.example.jp", "to_addr": OWN}
            target, _ = mail_threads.plan(c, ctx, m, [f"<p{16005}@x>", f"<p{5}@x>"])
            assert target == 5
        plan = (time.perf_counter() - t) / 200
        print(f"\n2万通: 最初の数え直し {first:.2f}秒・2回目 {again:.2f}秒・一覧の500スレッドを数える "
              f"{count * 1000:.1f}ms・新しいメール1通を決める {plan * 1000:.2f}ms")
        assert first < 15 and again < 10 and count < 0.5 and plan < 0.05
    finally:
        c.close()
