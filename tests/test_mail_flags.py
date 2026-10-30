"""★フラグ (みんなで共有する、目立たせたいメールの印) のテスト.

- POST /api/mail/{id}/flag: 付ける (on: true)・外す (on: false)。管理者もメール担当者も使える
  (状態は変えない)。もう付いているときに付けても、最初に付けた人・時刻のまま (記録も1回だけ)。
  外すと付けた人・時刻も消える。知らないメールは 404、トークンなし 401、勤怠だけの人 403
- 一覧・詳細に flagged・flagged_at・flagged_by が出る。?flagged=1 で★のメールだけ
  (検索・状態の絞り込みと一緒に効く)
- ★のメールは重要度「低」でも出す (「高・中」でも隠さず、X-Hidden-Low にも数えない)。
  確認待ち・差し戻した[低]も今までどおり出す
- タブの数 tabs.flagged は、「★ フラグ」のタブを押したときの一覧の通数と同じ
  (状態を選んでいればその状態だけ・担当・アドレスの絞り込みも効く)
- 保存日数を過ぎても★のメールは消さない (★を外すと、次の片付けで消える)。そのメールに Webメール・
  Gmail から送った返信 (送信箱) も残す。片付けがメールを選んだ後に★を付けても、そのメールは消さない。
  片付けが消している間に★を付けようとしたら、メールが無くなったと分かる (404)
- tabs.flagged_mark (一番新しく★を付けた時刻) で、数が同じでも★を付け替えたことに気付ける
- DB は列3つと索引1つを足すだけ (何度開いても同じ)。前の版のコードも、この DB で動く
- 画面: 行の ☆・返信画面の「☆ フラグ」・「★ フラグ」のタブ・状態の印・会社のタイムゾーンの日時
"""
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

ROOT = Path(__file__).resolve().parents[1]


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
    """勤怠だけの人・管理者・メール担当者."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
        staff = db.create_user(conn, "suzuki")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
    return worker, admin, staff


def _row(tmp_path, mid):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return dict(conn.execute("SELECT * FROM mails WHERE id = ?", (mid,)).fetchone())


def _set(tmp_path, mid, **cols):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE mails SET {sets} WHERE id = ?", (*cols.values(), mid))


def _audits(tmp_path, action):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def _flag(client, user, mid, on=True):
    return client.post(f"/api/mail/{mid}/flag", headers=auth(user), json={"on": on})


def _list(client, user, query=""):
    r = client.get("/api/mail" + query, headers=auth(user))
    assert r.status_code == 200, r.text
    return r


def _ids(client, user, query=""):
    return [m["id"] for m in _list(client, user, query).json()]


def _tabs(client, user, query=""):
    r = client.get("/api/mail/status" + query, headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()["tabs"]


# ---------- 付ける・外す ----------

def test_admin_and_staff_can_flag_and_unflag(client, users, tmp_path):
    _, admin, staff = users
    mid = _add_mail(tmp_path, subject="鍵の受け渡し")
    r = _flag(client, staff, mid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"] == mid and out["flagged"] is True and out["flagged_by"] == "suzuki"
    assert out["subject"] == "鍵の受け渡し" and out["flagged_at"]
    row = _row(tmp_path, mid)
    assert row["flagged_at"] == out["flagged_at"] and row["flagged_by"] == "suzuki"
    # ★は状態を変えない (未対応のまま)
    assert row["status"] == "unhandled"
    # 管理者が外す (みんなで共有する印なので、付けた人でなくても外せる)
    r = _flag(client, admin, mid, on=False)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": mid, "flagged": False, "flagged_at": None, "flagged_by": "",
                        "subject": "鍵の受け渡し"}
    row = _row(tmp_path, mid)
    assert row["flagged_at"] is None and row["flagged_by"] == ""
    # 管理者も付けられる
    assert _flag(client, admin, mid).json()["flagged_by"] == "boss"


def test_flag_twice_keeps_first_and_logs_once(client, users, tmp_path):
    """もう付いているときに付けても、最初に付けた人・時刻のまま (操作の記録も1回だけ)."""
    _, admin, staff = users
    mid = _add_mail(tmp_path)
    first = _flag(client, staff, mid).json()
    again = _flag(client, admin, mid)
    assert again.status_code == 200
    assert again.json()["flagged_at"] == first["flagged_at"]
    assert again.json()["flagged_by"] == "suzuki"
    a = _audits(tmp_path, "mail_flag")
    assert len(a) == 1 and a[0]["detail"] == f"mail={mid}" and a[0]["admin_id"] == users[2]["id"]
    # 外すと mail_unflag を残す。もう外れているときに外しても、何も残さない
    assert _flag(client, admin, mid, on=False).json()["flagged"] is False
    assert _flag(client, admin, mid, on=False).json()["flagged"] is False
    u = _audits(tmp_path, "mail_unflag")
    assert len(u) == 1 and u[0]["detail"] == f"mail={mid}" and u[0]["admin_id"] == admin["id"]
    # 操作の記録 (CSV) には分かりやすい名前で出る
    csv = client.get("/api/reports/audit.csv", headers=auth(admin)).text
    assert "メールに★フラグを付けた" in csv and "メールの★フラグを外した" in csv


def test_flag_refuses_unknown_mail_and_other_people(client, users, tmp_path):
    worker, admin, _ = users
    mid = _add_mail(tmp_path)
    assert _flag(client, admin, mid + 100).status_code == 404
    assert client.post(f"/api/mail/{mid}/flag", json={"on": True}).status_code == 401
    # 勤怠だけの人 (メール画面を使えない) も付けられない
    assert _flag(client, worker, mid).status_code == 403
    # on が無い・真偽でないときは 422
    assert client.post(f"/api/mail/{mid}/flag", headers=auth(admin), json={}).status_code == 422
    assert _row(tmp_path, mid)["flagged_at"] is None
    assert _audits(tmp_path, "mail_flag") == []


# ---------- 一覧・詳細 ----------

def test_summary_fields_in_list_and_detail(client, users, tmp_path):
    _, admin, _ = users
    a = _add_mail(tmp_path, n=1)
    b = _add_mail(tmp_path, n=2)
    _flag(client, admin, a)
    items = {m["id"]: m for m in _list(client, admin).json()}
    assert items[a]["flagged"] is True and items[a]["flagged_by"] == "boss"
    assert items[a]["flagged_at"] == _row(tmp_path, a)["flagged_at"]
    assert items[b]["flagged"] is False and items[b]["flagged_at"] is None
    assert items[b]["flagged_by"] == ""
    d = client.get(f"/api/mail/{a}", headers=auth(admin)).json()
    assert d["flagged"] is True and d["flagged_by"] == "boss" and d["flagged_at"]
    # 返信していない・対応不要でないメールは、誰が・いつを出さない
    for m in (items[a], d):
        assert m["replied_by"] == "" and m["reply_source"] == ""
        assert m["archived_at"] is None and m["archived_by"] == ""


def test_flagged_filter_alone_with_search_and_status(client, users, tmp_path):
    _, admin, _ = users
    a = _add_mail(tmp_path, n=1, subject="清掃の日程")
    b = _add_mail(tmp_path, n=2, subject="清掃のお見積もり", status="replied")
    c = _add_mail(tmp_path, n=3, subject="ご予約の確認")
    d = _add_mail(tmp_path, n=4, subject="清掃のご依頼")        # ★なし
    for mid in (a, b, c):
        _flag(client, admin, mid)
    assert _ids(client, admin, "?flagged=1") == [c, b, a]
    assert _ids(client, admin, "?flagged=1&q=" + "清掃") == [b, a]
    assert _ids(client, admin, "?flagged=1&status=replied") == [b]
    assert _ids(client, admin, "?flagged=1&status=unhandled") == [c, a]
    assert _ids(client, admin, "?flagged=0") == [d, c, b, a]
    # 外したら出なくなる
    _flag(client, admin, b, on=False)
    assert _ids(client, admin, "?flagged=1") == [c, a]


def test_flagged_low_mail_is_shown_with_hide_low(client, users, tmp_path):
    """★は目立たせたくて付けた印なので、重要度「低」でも隠さない (隠した数にも入れない)."""
    _, admin, _ = users
    low = _add_mail(tmp_path, n=1, priority=3, subject="お知らせ（★）")
    hidden = _add_mail(tmp_path, n=2, priority=3, subject="広告")
    returned = _add_mail(tmp_path, n=3, priority=3, subject="差し戻し")
    pending = _add_mail(tmp_path, n=4, priority=3, status="pending_approval")
    normal = _add_mail(tmp_path, n=5)
    _set(tmp_path, returned, return_note="宛名を直してください")
    r = _list(client, admin, "?hide_low=1")
    assert [m["id"] for m in r.json()] == [normal, pending, returned]
    assert r.headers["X-Hidden-Low"] == "2"
    _flag(client, admin, low)
    r = _list(client, admin, "?hide_low=1")
    # 確認待ち・差し戻した[低]も今までどおり出る (5e4452b の見張り)
    assert [m["id"] for m in r.json()] == [normal, pending, returned, low]
    assert r.headers["X-Hidden-Low"] == "1"
    assert hidden not in [m["id"] for m in r.json()]
    # 「★ フラグ」のタブ (flagged=1) では、[低]を隠していても全部出る
    r = _list(client, admin, "?flagged=1&hide_low=1")
    assert [m["id"] for m in r.json()] == [low] and r.headers["X-Hidden-Low"] == "0"
    # 種類のタブの数も、一覧と同じく★の[低]を数える
    t = _tabs(client, admin, "?hide_low=1")
    assert t["all"] == 4 and t["flagged"] == 1


# ---------- タブの数 ----------

def test_flag_tab_count_matches_list(client, users, tmp_path):
    """「★ フラグ」のタブの数は、押したときの一覧の通数と同じ (状態・担当・アドレスでも)."""
    from server import db
    _, admin, staff = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        acct = conn.execute(
            "INSERT INTO mail_accounts (label, imap_host, imap_user, imap_pass, created_at)"
            " VALUES ('予約用', 'imap.example.com', 'yoyaku@example.com', 'pw',"
            " '2026-10-01T00:00:00+00:00')").lastrowid
    mails = {
        "unhandled": _add_mail(tmp_path, n=1),
        "pending_approval": _add_mail(tmp_path, n=2, status="pending_approval", priority=3),
        "replied": _add_mail(tmp_path, n=3, status="replied", priority=3),
        "archived": _add_mail(tmp_path, n=4, status="archived"),
    }
    extra = _add_mail(tmp_path, n=5, status="replied")
    _add_mail(tmp_path, n=6)    # ★なし
    for mid in [*mails.values(), extra]:
        _flag(client, admin, mid)
    _set(tmp_path, mails["unhandled"], assignee_user_id=staff["id"], account_id=acct)
    _set(tmp_path, extra, account_id=acct)
    assert _tabs(client, admin)["flagged"] == 5
    for low in ("", "&hide_low=1"):
        for st in mails:
            q = f"?status={st}{low}"
            assert _tabs(client, admin, q)["flagged"] == len(_ids(client, admin, q + "&flagged=1")), q
        assert _tabs(client, admin, "?status=replied" + low)["flagged"] == 2
        assert _tabs(client, admin, "?" + low)["flagged"] == len(_ids(client, admin, "?flagged=1" + low)) == 5
    # 担当・アドレスの絞り込み
    assert _tabs(client, staff, "?assignee=me")["flagged"] == 1
    assert _tabs(client, admin, f"?account={acct}")["flagged"] == 2
    assert len(_ids(client, admin, f"?account={acct}&flagged=1")) == 2
    assert _tabs(client, admin, f"?account={acct}&status=replied")["flagged"] == 1
    assert _tabs(client, admin, "?assignee=none")["flagged"] == 4
    # ★の数が変わると、ほかの画面も1分以内に気付く (tabs が変わる)
    _flag(client, admin, extra, on=False)
    assert _tabs(client, admin)["flagged"] == 4


def test_flag_tab_changes_when_star_moves_to_another_mail(client, users, tmp_path):
    """1通の★を外して別の1通に付けると、★の数は同じでも tabs が変わる (ほかの画面が取り直す)."""
    _, admin, staff = users
    a = _add_mail(tmp_path, n=1)
    b = _add_mail(tmp_path, n=2)
    assert _tabs(client, admin)["flagged_mark"] == ""
    _flag(client, admin, a)
    before = _tabs(client, staff)
    assert before["flagged"] == 1 and before["flagged_mark"] == _row(tmp_path, a)["flagged_at"]
    _flag(client, admin, a, on=False)
    _flag(client, admin, b)
    after = _tabs(client, staff)
    assert after["flagged"] == 1
    assert after["flagged_mark"] == _row(tmp_path, b)["flagged_at"]
    assert after != before
    # 絞り込みも数と同じに効く (返信済みだけを選んでいれば、返信済みの★だけ)
    assert _tabs(client, admin, "?status=replied")["flagged_mark"] == ""


def test_flag_count_uses_small_index(client, users, tmp_path):
    """★の数は、★のメールだけの小さい索引で数える (状態・担当で絞っても、返信済み全部を読まない)."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        for where in ("m.flagged_at IS NOT NULL",
                      "m.flagged_at IS NOT NULL AND m.status = 'replied'",
                      "m.flagged_at IS NOT NULL AND m.assignee_user_id IS NULL"
                      " AND m.account_id = 1 AND m.status = 'archived'"):
            plan = " ".join(str(tuple(r)) for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*), MAX(m.flagged_at) FROM mails m"
                " INDEXED BY idx_mails_flagged WHERE " + where))
            assert "idx_mails_flagged" in plan, plan
        # 何も絞らなければ、索引だけで数える
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*), MAX(m.flagged_at) FROM mails m"
            " WHERE m.flagged_at IS NOT NULL"))
        assert "COVERING INDEX idx_mails_flagged" in plan, plan
    src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    assert '"SELECT COUNT(*), MAX(m.flagged_at) FROM mails m INDEXED BY idx_mails_flagged WHERE "' in src


# ---------- 保存日数 ----------

def test_purge_keeps_flagged_mail_until_unflagged(client, users, tmp_path):
    from server import db, mail
    _, admin, _ = users
    old = "2026-01-01T00:00:00+00:00"
    keep = _add_mail(tmp_path, n=1, received_at=old)
    gone = _add_mail(tmp_path, n=2, received_at=old)
    fresh = _add_mail(tmp_path, n=3, received_at="2099-01-01T00:00:00+00:00")
    _flag(client, admin, keep)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30) == 1
        ids = [r[0] for r in conn.execute("SELECT id FROM mails ORDER BY id")]
    assert ids == [keep, fresh] and gone not in ids
    # ★を外すと、次の片付けで消える
    _flag(client, admin, keep, on=False)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30) == 1
        assert [r[0] for r in conn.execute("SELECT id FROM mails")] == [fresh]


class _FlagAfterFirstChunk:
    """purge_old_mails に渡す接続。1通目を消して確定した直後に、ほかの人が別のメールに★を付ける
    (片付けがメールを選んだ後・そのメールを消す前に★を付けたとき)."""

    def __init__(self, conn, first, flag):
        self._conn, self._first, self._flag = conn, first, flag

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        self._conn.commit()
        if self._flag and not self._conn.execute(
                "SELECT 1 FROM mails WHERE id = ?", (self._first,)).fetchone():
            flag, self._flag = self._flag, None
            flag()


def test_purge_keeps_mail_flagged_after_it_was_picked(client, users, tmp_path, monkeypatch):
    """片付けが消すメールを選んだ後に★を付けても、そのメールは添付ファイルごと残る."""
    from server import db, mail
    _, admin, staff = users
    monkeypatch.setattr(mail, "PURGE_CHUNK", 1)
    att_dir = tmp_path / "mail_attachments"
    old = "2026-01-01T00:00:00+00:00"
    first = _add_mail(tmp_path, n=1, received_at=old)
    late = _add_mail(tmp_path, n=2, received_at=old)
    for mid in (first, late):
        (att_dir / str(mid)).mkdir(parents=True)
        (att_dir / str(mid) / "1").write_bytes(b"pdf")
    flagged = []

    def flag_late():
        r = _flag(client, staff, late)
        assert r.status_code == 200, r.text
        flagged.append(r.json()["flagged"])

    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(_FlagAfterFirstChunk(conn, first, flag_late), 30, att_dir) == 1
        assert [r[0] for r in conn.execute("SELECT id FROM mails")] == [late]
    assert flagged == [True]
    assert _row(tmp_path, late)["flagged_by"] == "suzuki"
    assert (att_dir / str(late) / "1").read_bytes() == b"pdf"
    assert not (att_dir / str(first)).exists()


def test_flag_while_purge_is_deleting_the_mail(client, users, tmp_path, monkeypatch):
    """片付けが消している途中に★を付けようとしたら、消し終えるのを待って 404 (サーバのエラーにしない)."""
    import threading
    import time
    from server import db, mail
    _, admin, _ = users
    att_dir = tmp_path / "mail_attachments"
    mid = _add_mail(tmp_path, n=1, received_at="2026-01-01T00:00:00+00:00")
    (att_dir / str(mid)).mkdir(parents=True)
    out, threads = [], []
    remove = mail.remove_attachment_files

    def remove_while_flagging(attachment_dir, mail_id, sent=False):
        # ファイルを消している間 (書き込みのロックを持っている) に、ほかの人が★を押す
        t = threading.Thread(target=lambda: out.append(_flag(client, admin, mid)))
        t.start()
        time.sleep(0.5)
        remove(attachment_dir, mail_id, sent)
        threads.append(t)

    monkeypatch.setattr(mail, "remove_attachment_files", remove_while_flagging)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30, att_dir) == 1
    for t in threads:
        t.join(10)
    assert [(r.status_code, r.json()["detail"]) for r in out] == [
        (404, "メールが見つかりません（保存日数を過ぎて消えた可能性があります）")]
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 0
    assert not (att_dir / str(mid)).exists()
    assert _audits(tmp_path, "mail_flag") == []


def test_purge_keeps_replies_of_flagged_mail(client, users, tmp_path):
    """★のメールに Webメール・Gmail から送った返信 (送信箱) も、保存日数を過ぎても残す."""
    from server import db, mail
    _, admin, _ = users
    old = "2026-01-01T00:00:00+00:00"
    keep = _add_mail(tmp_path, n=1, received_at=old)
    gone = _add_mail(tmp_path, n=2, received_at=old)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        def sent(n, linked=None):
            return conn.execute(
                "INSERT INTO sent_mails (message_id, subject, sent_at, fetched_at, linked_mail_id)"
                " VALUES (?, ?, ?, ?, ?)", (f"<s{n}@x>", f"送った{n}", old, old, linked)).lastrowid
        reply = sent(1)                  # 返信済みにした返信 (reply_sent_id)
        linked = sent(2, keep)           # そのメールにつながった、もう1通の返信
        other = sent(3)                  # ほかのメールへの返信
        gone_reply = sent(4, gone)       # ★の無いメールへの返信
        conn.execute("UPDATE mails SET status = 'replied', replied_at = ?, reply_source = 'webmail',"
                     " reply_sent_id = ? WHERE id = ?", (old, reply, keep))
    _flag(client, admin, keep)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30) == 1
        assert [r[0] for r in conn.execute("SELECT id FROM mails")] == [keep]
        assert [r[0] for r in conn.execute("SELECT id FROM sent_mails ORDER BY id")] == [reply, linked]
    assert other and gone_reply
    # ★のメールの「送信箱で見る」は、送ったメールを開ける
    d = client.get(f"/api/mail/{keep}", headers=auth(admin)).json()
    assert d["reply_sent_key"] == f"i{reply}"
    assert client.get(f"/api/mail/sent/i{reply}", headers=auth(admin)).status_code == 200
    # ★を外すと、次の片付けで返信も一緒に消える
    _flag(client, admin, keep, on=False)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30) == 1
        assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sent_mails").fetchone()[0] == 0


# ---------- DB の移し替え ----------

def test_migration_adds_columns_and_index_once(tmp_path):
    """前の版の DB (★の列が無い) に、列3つと索引1つを足す。2回目に開いても何もしない."""
    from server import db
    path = tmp_path / "zatsumu.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO mails (message_id, received_at, fetched_at, status, archived_at)"
                 " VALUES ('<a@x>', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00',"
                 " 'archived', '2026-10-02T00:00:00+00:00')")
    # 前の版の形にする (索引と列を消す)
    conn.execute("DROP INDEX idx_mails_flagged")
    for col in ("flagged_at", "flagged_by", "archived_by"):
        conn.execute(f"ALTER TABLE mails DROP COLUMN {col}")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(mails)")]
    assert "flagged_at" not in cols and "archived_by" not in cols
    conn.close()

    conn = db.connect(path)
    try:
        info = {r[1]: (r[2], r[3], r[4]) for r in conn.execute("PRAGMA table_info(mails)")}
        assert info["flagged_at"] == ("TEXT", 0, None)
        assert info["flagged_by"] == ("TEXT", 1, "''") and info["archived_by"] == ("TEXT", 1, "''")
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_mails_flagged'").fetchone()[0]
        assert "WHERE flagged_at IS NOT NULL" in sql
        # 前からあるメールは★なし・対応不要にした人は空 (書き足さない)
        r = conn.execute("SELECT flagged_at, flagged_by, archived_by FROM mails").fetchone()
        assert tuple(r) == (None, "", "")
    finally:
        conn.close()
    conn = db.connect(path)
    try:
        assert conn.total_changes == 0
    finally:
        conn.close()


def _old_code_dir(tmp_path, rev="8f7ca2f"):
    """★フラグより前の版 (本番で動いていた版) のコードを取り出す (無ければテストを飛ばす)."""
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
    env.pop("ZATSUMU_DEMO", None)
    env.pop("ZATSUMU_LOGIN_VERIFY_EMAIL", None)
    head = f"import sys, json\nsys.path.insert(0, {str(old_dir)!r})\n"
    proc = subprocess.run([sys.executable, "-c", head + textwrap.dedent(script)],
                          cwd=str(old_dir), env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_previous_version_still_runs_on_new_db(tmp_path):
    """自動更新で前の版に戻っても、この版の DB で動く (★は知らないので、保存日数で消す)."""
    old_dir = _old_code_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    from server import db
    conn = db.connect(data / "zatsumu.db")
    old = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO mails (message_id, subject, received_at, fetched_at, priority,"
                 " flagged_at, flagged_by) VALUES ('<a@x>', '★の低', ?, ?, 3, ?, 'boss')",
                 (old, old, old))
    conn.commit()
    conn.close()
    out = _run_old(old_dir, data, f"""
        from server import db, mail
        conn = db.connect({str(data / "zatsumu.db")!r})
        conn.execute("INSERT INTO mails (message_id, subject, received_at, fetched_at)"
                     " VALUES ('<b@x>', '前の版', '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00')")
        conn.execute("UPDATE mails SET status = 'archived', archived_at = '2099-01-02T00:00:00+00:00'"
                     " WHERE message_id = '<b@x>'")
        conn.commit()
        purged = mail.purge_old_mails(conn, 30)
        rows = [list(r) for r in conn.execute("SELECT subject, archived_by FROM mails")]
        print(json.dumps({{"purged": purged, "rows": rows}}))
    """)
    assert out == {"purged": 1, "rows": [["前の版", ""]]}
    # この版に戻すと、前の版が対応不要にしたメールは、対応不要にした人が空のまま (日付だけ出す)
    conn = db.connect(data / "zatsumu.db")
    try:
        r = conn.execute("SELECT archived_by, flagged_at FROM mails").fetchone()
        assert tuple(r) == ("", None)
    finally:
        conn.close()


# ---------- 画面 ----------

def test_mail_page_has_star_tab_and_reply_screen_flag(client):
    page = client.get("/mail").text
    # タブ: 「重要（高）」のすぐ後に、いつも出す (数が無い前の版のサーバでも、数を出さずに出す)
    assert 'TAB_FLAG = "flag"' in page
    assert ('[TAB_HIGH, "重要（高）", t && t.high],\n                [TAB_FLAG, "★ フラグ", t && t.flagged],'
            in page)
    assert 'else if (activeTab === TAB_FLAG) q.set("flagged", "1");' in page
    assert 'key === TAB_FLAG ? " fl" : ""' in page and ".ctab.fl[aria-selected=\"true\"]" in page
    assert "★フラグを付けたメール ${Number(n) || 0} 件（上の「状態」で選んでいるときは、その状態だけ）" in page
    assert "★フラグを付けたメールはありません（一覧の ☆ を押すと付けられます" in page
    # 行の ☆ (差出人の左)。返信画面を開かず、行が動いた直後は押しても何もしない
    r = page[page.index("function renderInbox("):page.index('$("filter").onchange')]
    assert '<td class="c-from" data-label="差出人">${starButton(m)}<strong>' in r
    assert '${m.flagged ? " flagged" : ""}' in r
    assert 'class="row-flag" data-flag="${Number(m.id)}" aria-pressed="${t.on}"' in page
    m = re.search(r'tr\.onclick = e => \{(.*?)\n    \}\);', r, re.S).group(1)
    assert 'const f = e.target.closest("button.row-flag");' in m
    assert m.index("e.stopPropagation()") < m.index("ROW_QUIET_MS") < m.index("toggleFlag(")
    assert 'toggleFlag(Number(f.dataset.flag), f.getAttribute("aria-pressed") !== "true")' in m
    # キーボードで ☆ にいたら、描き直した後も ☆ に戻す
    keys = re.search(r"const ROW_BTN_KEYS = \[(.*?)\];", page).group(1)
    assert '"data-flag"' in keys
    # 返信画面の「☆ フラグ」は上の列の先頭 (前の版のサーバでは出さない)
    top = page[page.index('<div class="m-top-act">'):page.index('id="m-close-top"')]
    assert top.index('id="m-flag"') < top.index('id="m-archive"')
    assert '<button id="m-flag" type="button" class="ghost m-flag" aria-pressed="false" hidden>' in top
    f = page[page.index("function renderModalFlag(m) {"):]
    f = f[:f.index("\n}")]
    assert 'setFlagButton($("m-flag"), m);' in f and '$("m-meta").textContent = modalMeta(m);' in f
    # 「☆ フラグ」のボタンは返信画面と右の本文で同じ (setFlagButton)
    b = page[page.index("function setFlagButton(b, m) {"):]
    b = b[:b.index("\n}")]
    assert 'b.hidden = !("flagged" in m);' in b and '"★" : "☆"' in b and '" フラグ中" : " フラグ"' in b
    assert "toggleFlag(modal.id, !modal.mail.flagged)" in page
    # iPhone では、★を付けた人を件名の下の行にも出す (指して説明を見られないので)
    meta = page[page.index("function modalMeta(m) {"):page.index("function renderModalFlag(")]
    assert '(m.flagged ? `・★フラグ${m.flagged_by ? `（${m.flagged_by}）` : ""}` : "")' in meta
    # iPhone の縦では ☆・★ だけ (押しやすい大きさ)
    assert ".m-top .m-flag { width: 44px; padding: 0;" in page
    assert ".m-flag .m-flag-txt { display: none; }" in page
    # 指で押す画面では、行の ☆ も、「★ フラグ」などのタブも押しやすい大きさ
    assert ".row-flag { width: 40px; height: 40px;" in page
    assert "@media (pointer: coarse), (max-width: 640px) { .ctab { min-height: 40px; } }" in page


def test_mail_page_toggles_flag_without_reloading_list(client):
    """押したらすぐ見た目を変え、送れなかったら戻す。続けて押しても1回だけ送る."""
    page = client.get("/mail").text
    t = re.search(r"async function toggleFlag\(id, on\) \{(.*?)\n\}", page, re.S).group(1)
    # 送っている間や、押してすぐ (ROW_QUIET_MS の間) に同じメールを押しても何もしない
    # (2回続けて押しても、付けてすぐ外れない。送り終えるのが速くても同じ)
    assert ("if (flagBusy.has(id) || Date.now() - (flagTapAt.get(id) || 0) < ROW_QUIET_MS) return;\n"
            "  flagBusy.add(id);\n  flagTapAt.set(id, Date.now());") in t
    # 送る前に見た目を変える (待たせない)
    assert t.index("setFlagUi(id, on ? new Date().toISOString() : null, on ? me.name : \"\");") < \
        t.index("await apiJson(`/api/mail/${id}/flag`")
    assert "if (!r) { setFlagUi(id, ...before); return; }" in t
    assert "setFlagUi(id, r.flagged_at, r.flagged_by);" in t
    # 一覧はすぐには取り直さない (行が動かない)。タブの数はそろえ、ほかの変化は見逃さない
    assert "showInbox(" not in t and "refreshInbox(" not in t
    assert "try { if (await checkStatus()) inboxStale = true; } catch {}" in t
    s = page[page.index("function setFlagUi(id, at, by) {"):page.index("async function toggleFlag(")]
    # 行の ☆・行の黄色い線・返信画面・覚えている一覧の項目をそろえる
    assert 'button.row-flag[data-flag="${Number(id)}"]' in s
    assert 'mailRowOf(id)?.classList.toggle("flagged", f.flagged);' in s
    assert "if (m) Object.assign(m, f);" in s and "renderModalFlag(modal.mail);" in s
    # 押す前に頼んでいた一覧の答えが、★を送り終えた後に届いても、この画面の★で描く
    # (古い★に戻さない。動きは tests/test_mail_list_ui.py)
    assert "flagLocal.set(id, { at: Date.now(), f });" in s
    show = page[page.index("async function showInbox("):page.index("function loadSeen(")]
    assert show.index("const askedAt = Date.now();") < show.index("await api(path)")
    assert show.index("applyLocalFlags(list, askedAt);") < show.index("renderInbox(list, opts.auto);")


def test_mail_page_status_marks_and_dates(client):
    """状態の欄は statusMark (↩ 返信済み・✓ 対応不要 といつ・誰が)。日時は会社のタイムゾーン."""
    page = client.get("/mail").text
    r = page[page.index("function renderInbox("):page.index('$("filter").onchange')]
    assert '<td class="c-st" data-label="状態">${statusMark(m)}</td>' in r
    assert '<td class="c-date" data-label="受信日時">${listTime(m.received_at)}</td>' in r
    assert "${repliedIcon(m)}${hl(m.subject, T)" in r
    s = re.search(r"function showRowArchived\(id, r = \{\}\) \{(.*?)\n\}", page, re.S).group(1)
    assert "statusMark(" in s
    # ブラウザの時計のタイムゾーン (toLocaleString のまま) では出さない
    assert 'toLocaleString("ja-JP")' not in r
    c = page[page.index("function modalMeta(m) {"):page.index("function renderApprovalBox(")]
    assert 'toLocaleString("ja-JP")' not in c and "${fmtDate(m.received_at)}" in c
    assert "function fmtListDate(iso, now = new Date()) {" in page
    assert ".d-short { display: none; }" in page
    # 灰色の行でも印ははっきり。★の行は右端に黄色の線 (スマホのカードも)
    assert ".mail-row.done .st-chip { opacity: 1; }" in page
    # いつ・誰が は欄に入らなければ折り返す (状態の欄が広がって、表が横にはみ出さない)。日時は折り返さない
    sub = re.search(r"\n  \.st-sub \{([^}]*)\}", page).group(1)
    assert "nowrap" not in sub and "overflow-wrap: anywhere" in sub
    assert ".st-sub .st-when { white-space: nowrap; }" in page
    assert ".mail-row.flagged td.act { box-shadow:" in page
    assert ".table-wrap tr.mail-row.flagged { border-right: 5px solid #eab308; }" in page


def test_mail_page_explains_flags(client):
    page = client.get("/mail").text
    howto = page[page.index('data-howto="inbox"'):]
    howto = howto[:howto.index("</details>")]
    for text in ("行の左の ☆ を押すと ★ になります",
                 "返信画面の上の「☆ フラグ」でも付けられます",
                 "誰がいつ付けたかも残ります",
                 "「★ フラグ」のタブで ★ のメールだけを見られます",
                 "メールの保存日数を過ぎても消しません",
                 "確認待ち・差し戻したメール・★のメールは低でも出します",
                 "返信したメールには「↩ 返信済み」と、いつ・誰が返信したかが出ます",
                 "対応不要にしたメールには「✓ 対応不要」と、いつ・誰がしたかが出ます"):
        assert text in howto, text
    # ★の説明はメール担当者にも出す (管理者だけの説明の外)
    assert howto.index("行の左の ☆") < howto.index('<span id="admin-help">')
    # 設定の「メール保存日数」の横
    s = page[page.index('id="set-mail-retention"'):]
    assert "★フラグを付けたメールは、日数を過ぎても消しません（★を外すと、次の片付けで消えます）" in s[:400]
