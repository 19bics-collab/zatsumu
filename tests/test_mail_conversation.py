"""受信箱のプルダウン (「本文 ▼」で本文とその後のやり取りを見る) のテスト.

- /api/mail/{id}/conversation: このメールの本文と、この後のやり取り (こちらの返信・後から届いた
  同じ相手のメール・ヘッダでつながる別のアドレスからのメール) を古い順に返す
- 前のやり取りは頼まれたときだけ (それまでは数だけ)
- 件数・本文の長さの上限、同じ時刻のメールの前後、相手が分からないメール
- メール担当者も見られる・一般メンバーは見られない (役割の表は test_mail_approval)
- 索引で探す (メール全部を読まない)
- 画面: 行の「本文 ▼」・プルダウンの中のボタン・自動の取り直しで閉じないこと

ネットワークには出ない。
"""
import re

import pytest

from tests.test_api import auth
from tests.test_mail_thread import _mail, _set, client, reset_tz, users  # noqa: F401


def _convo(client, user, mid, earlier=False):
    r = client.get(f"/api/mail/{mid}/conversation" + ("?earlier=true" if earlier else ""),
                   headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


def _kinds(events):
    return [(e["kind"], e["mail_id"]) for e in events]


# ---------- この後のやり取り ----------

def test_after_has_our_reply_and_later_mails_in_time_order(client, users, tmp_path):
    _, admin = users
    a1 = _mail(tmp_path, 1, from_addr="guest@example.com", body="最初の問い合わせ")
    a2 = _mail(tmp_path, 2, from_addr="guest@example.com", body="二通目の本文\n2行目")
    a3 = _mail(tmp_path, 3, from_addr="GUEST@example.com", body="三通目")
    a4 = _mail(tmp_path, 4, from_addr="guest@example.com", body="四通目")
    _mail(tmp_path, 5, from_addr="other@example.com")          # 別の相手は入れない
    # a2 に返信 (a3 が届く前)、a3 にも返信 (a4 が届いた後)
    _set(tmp_path, a2, status="replied", reply_body="ご連絡ありがとうございます",
         replied_at="2026-08-12T05:00:00+00:00", replied_by="黒須",
         reply_sent_to="guest@example.com", reply_cc="boss@bics.example")
    _set(tmp_path, a3, status="replied", reply_body="承知しました",
         replied_at="2026-08-14T09:00:00+00:00", replied_by="田中")
    d = _convo(client, admin, a2)
    # このメールの本文は全部
    assert d["id"] == a2 and d["body"] == "二通目の本文\n2行目"
    assert d["contact_addr"] == "guest@example.com" and d["status"] == "replied"
    # 時刻順: a2 への返信 → a3 → a4 (14日0時) → a3 への返信 (14日9時)
    assert _kinds(d["after"]) == [("out", a2), ("in", a3), ("in", a4), ("out", a3)]
    out = d["after"][0]
    assert out["body"] == "ご連絡ありがとうございます" and out["replied_by"] == "黒須"
    assert out["sent_to"] == "guest@example.com" and out["cc"] == ["boss@bics.example"]
    assert out["at"] == "2026-08-12T05:00:00+00:00"
    later = d["after"][1]
    assert later["body"] == "三通目" and later["subject"] == "件名3"
    assert later["status"] == "replied" and later["contact_addr"] == "GUEST@example.com"
    assert d["after_total"] == 2 and d["shown_after"] == 2
    # 前のやり取りは頼まれるまで数だけ
    assert d["earlier"] == [] and d["earlier_total"] == 1
    # 頼まれたら前のやり取りも (古い順)
    e = _convo(client, admin, a2, earlier=True)
    assert _kinds(e["earlier"]) == [("in", a1)] and e["earlier"][0]["body"] == "最初の問い合わせ"
    assert _kinds(e["after"]) == _kinds(d["after"])
    # 一番新しいメールなら、この後は空
    assert _convo(client, admin, a4)["after"] == []
    assert _convo(client, admin, a4)["earlier_total"] == 3


def test_reply_to_the_mail_itself_is_shown_even_without_later_mails(client, users, tmp_path):
    _, admin = users
    a = _mail(tmp_path, 1, from_addr="once@example.com")
    _set(tmp_path, a, status="replied", reply_body="返信しました",
         replied_at="2026-08-11T03:00:00+00:00", replied_by="黒須")
    d = _convo(client, admin, a)
    # 送った宛先が残っていない (前の版で送った) ときは差出人
    assert _kinds(d["after"]) == [("out", a)] and d["after"][0]["sent_to"] == "once@example.com"
    assert d["after_total"] == 0 and d["earlier_total"] == 0


def test_mails_linked_by_headers_from_other_addresses(client, users, tmp_path):
    _, admin = users
    a = _mail(tmp_path, 1, message_id="<a@x.example>", from_addr="cust@example.com")
    # 同じ話への、別のアドレス (上司など) からの返事
    b = _mail(tmp_path, 2, from_addr="boss@cust.example", refs="<a@x.example>")
    c = _mail(tmp_path, 3, from_addr="unrelated@example.com")
    d = _convo(client, admin, a)
    assert _kinds(d["after"]) == [("in", b)] and d["after_total"] == 1
    assert c not in [ev["mail_id"] for ev in d["after"]]
    # 返事の側から見ると、元のメールは前のやり取り
    e = _convo(client, admin, b, earlier=True)
    assert _kinds(e["earlier"]) == [("in", a)] and e["earlier_total"] == 1


def test_same_time_mails_are_ordered_by_number(client, users, tmp_path):
    _, admin = users
    t = "2026-08-20T00:00:00+00:00"
    a = _mail(tmp_path, 1, from_addr="same@example.com", received_at=t)
    b = _mail(tmp_path, 2, from_addr="same@example.com", received_at=t)
    c = _mail(tmp_path, 3, from_addr="same@example.com", received_at=t)
    d = _convo(client, admin, b, earlier=True)
    assert _kinds(d["after"]) == [("in", c)] and _kinds(d["earlier"]) == [("in", a)]
    assert d["after_total"] == d["earlier_total"] == 1


def test_mails_without_counterpart_are_not_grouped(client, users, tmp_path):
    _, admin = users
    a = _mail(tmp_path, 1, from_addr="")
    _mail(tmp_path, 2, from_addr="")
    d = _convo(client, admin, a, earlier=True)
    assert d["after"] == [] and d["earlier"] == []
    assert d["after_total"] == d["earlier_total"] == 0


def test_counts_and_bodies_are_capped(client, users, tmp_path, monkeypatch):
    from server import mail
    _, admin = users
    monkeypatch.setattr(mail, "CONVO_MAX", 3)
    monkeypatch.setattr(mail, "CONVO_BODY_CHARS", 10)
    long_body = "あ" * 50
    ids = [_mail(tmp_path, n, from_addr="many@example.com", body=long_body,
                 received_at=f"2026-08-01T00:{n:02d}:00+00:00") for n in range(1, 11)]
    d = _convo(client, admin, ids[4], earlier=True)
    # この後は直後から3通、前は直前の3通 (どちらも古い順)
    assert _kinds(d["after"]) == [("in", i) for i in ids[5:8]]
    assert _kinds(d["earlier"]) == [("in", i) for i in ids[1:4]]
    assert (d["after_total"], d["shown_after"]) == (5, 3)
    assert (d["earlier_total"], d["shown_earlier"]) == (4, 3)
    # ほかのメールの本文は途中まで (印付き)。このメール自身は全部
    assert d["after"][0]["body"] == "あ" * 10 and d["after"][0]["body_cut"] is True
    assert d["body"] == long_body


def test_conversation_404_and_permissions(client, users, tmp_path):
    from server import db
    worker, admin = users
    a = _mail(tmp_path, 1)
    assert client.get("/api/mail/99999/conversation", headers=auth(admin)).status_code == 404
    assert client.get(f"/api/mail/{a}/conversation").status_code == 401
    assert client.get(f"/api/mail/{a}/conversation", headers=auth(worker)).status_code == 403
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (worker["id"],))
    # メール担当者は見られる (返信画面と同じ)
    assert _convo(client, worker, a)["id"] == a
    # 番号でない (返信画面の /api/mail/{id} とぶつからない)
    assert client.get("/api/mail/abc/conversation", headers=auth(admin)).status_code == 422


def test_conversation_queries_use_indexes(tmp_path):
    """プルダウンの問い合わせは、メール全部を読まない (索引で探す)."""
    from server import db, mail
    a = _mail(tmp_path, 1, message_id="<a@x.example>", from_addr="cust@example.com")
    _set(tmp_path, a, reply_message_id="<r@bics.example>")
    mid = _mail(tmp_path, 2, from_addr="cust@example.com")
    _mail(tmp_path, 3, from_addr="boss@cust.example", refs="<a@x.example>")
    _mail(tmp_path, 4, from_addr="CUST@example.com", in_reply_to="<r@bics.example>")
    for n in range(5, 40):
        _mail(tmp_path, n, from_addr=f"u{n}@example.com", refs=f"<t{n}@x.example>")
    conn = db.connect(tmp_path / "zatsumu.db")
    conn.execute("ANALYZE")
    statements = []
    conn.set_trace_callback(statements.append)
    row = conn.execute("SELECT * FROM mails WHERE id = ?", (mid,)).fetchone()
    d = mail.mail_conversation(conn, row, earlier=True)
    conn.set_trace_callback(None)
    assert len(d["after"]) == 2 and len(d["earlier"]) == 1
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) >= 5
    for sql in selects:
        plan = [r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert not [p for p in plan if p.startswith("SCAN")], (sql, plan)
    conn.close()


# ---------- 画面 ----------

def test_mail_page_has_peek_dropdown(client):
    page = client.get("/mail").text
    # 行の「本文 ▼」(開いているかを読み上げにも伝える)。押しても返信画面は開かない
    assert 'class="row-peek"' in page and 'aria-expanded="${open}"' in page
    assert 'aria-controls="peek-${id}"' in page and "本文 ${open ? \"▲\" : \"▼\"}" in page
    m = re.search(r'tr\.onclick = e => \{(.*?)\n    \}\);', page, re.S)
    assert m and 'e.target.closest("button.row-peek")' in m.group(1)
    body = m.group(1)
    assert body.index("row-peek") < body.index("openMail(")
    assert "e.stopPropagation();\n        togglePeek(" in body
    # 軽い API を使い、前のやり取りは押したときだけ読む
    assert "/conversation${want ? \"?earlier=true\" : \"\"}" in page
    for text in ("この後のやり取り", "↩ こちらの返信", "前のやり取りも見る（", "返信画面を開く",
                 "前のやり取り（古い順）", 'role="region" aria-label="本文とその後のやり取り"'):
        assert text in page, text
    # プルダウンの中の「対応不要」も同じボタン (管理者の未対応のメールだけ)・同じ押し間違い対策
    inner = page[page.index("function peekInner("):page.index("function fillPeek(")]
    assert 'isAdmin() && m.status === "unhandled" ? archiveButton(m) : ""' in inner
    click = page[page.index('document.querySelector("#mail-table tbody").addEventListener("click"'):]
    click = click[:click.index("\n});")]
    assert "Date.now() - rowsMovedAt < ROW_QUIET_MS) return;" in click
    assert click.index("ROW_QUIET_MS") < click.index("archiveFromRow(b, tr)")
    # 対応不要で行が消えるときは、開いているプルダウンも一緒に動かして消す
    a = page[page.index("async function archiveFromRow(b, tr) {"):]
    a = a[:a.index("\n}")]
    assert 'pk?.classList.add("leaving")' in a and "removeRows([tr, pk], " in a
    # 行・プルダウンを消すとき (閉じる・対応不要) と、画面より上のプルダウンが伸びたときは、
    # どの機種でも画面の手で見ている場所を合わせる (ブラウザの自動の合わせ方は受信箱では止めて、
    # 二重に動かない・iPhone の Safari で何通も先のメールが見えてしまわないように)
    assert "html.inbox-on { overflow-anchor: none; }" in page and '<html lang="ja" class="inbox-on">' in page
    assert 'document.documentElement.classList.toggle("inbox-on", name === "inbox");' in page
    rm = page[page.index("function removeRows(els, keep) {"):page.index("function fillPeek(")]
    assert "if (top >= 0) return;" in rm and 'keep.getBoundingClientRect().top - 8' in rm
    assert "if (!open) { removeRows([peekRowOf(id)], tr); return; }" in page
    assert ".mail-row.leaving, .peek-row.leaving {" in page
    # 自動の取り直し・タブの切り替えで閉じない (開いている行を覚えて描き直す)。
    # 行が変わったときだけ読み直す
    r = page[page.index("function renderInbox("):page.index('$("filter").onchange')]
    assert "${peeking ? peekRowHtml(id) : \"\"}" in r and "c.sig !== peekSig(m)" in r
    assert '.find(tr => tr.getBoundingClientRect().bottom > 0)' in r and '".mail-row, .peek-row"' in r
    # 外から来た文字は esc() を通す
    ev = page[page.index("function pkEvent("):page.index("function peekInner(")]
    # 本文は bodyHtml (引用を畳み、文字は esc() を通す。tests/test_mail_quote.py)
    for safe in ("${bodyHtml(e.body, ", "${esc(e.subject)", "${esc(contactLine(e))}",
                 "esc(e.replied_by)", "${esc(e.sent_to)}", 'esc(e.cc.join(", "))'):
        assert safe in ev, safe
    assert "${e.body" not in ev and "${e.subject" not in ev and "${d.body" not in inner
    assert "${bodyHtml(d.body, " in inner
    # 本文は折り返し、中だけのスクロール枠にしない (プルダウンの .mail-body も同じ決まり)
    css = page[page.index("  .peek {"):page.index("  .pk-more {")]
    assert "max-height" not in css and "overflow: auto" not in css
    # 表が横にはみ出す幅でも、プルダウンは見えている幅に収める
    assert "position: sticky; left: 0; max-width: var(--peek-w, 100%);" in page
    assert 'wrap.style.setProperty("--peek-w"' in page
