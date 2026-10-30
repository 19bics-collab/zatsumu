"""受信箱のスレッドの API のテスト (/api/mail の th_*・/api/mail/threads/{key}・/api/mail/threads-check).

- 一覧はメールのまま (同じメール・同じ並び・同じ数)。それぞれにスレッドの通数などを付けるだけ
- 探している間・設定でスレッドを止めているときは、どのメールも1通だけのスレッド (X-Thread-Mode: 0)
- スレッドのメールは、つながりの木の順に深さと返事の元を付けて返す。多いときは最初のメールと新しい方から100通
- 管理者の確かめ (threads-check)。メール担当者は使えない
- 前の版の DB に足すもの (列・表・索引・トリガー4つ)。2回目は何もしない。前の版の書き方の SQL もそのまま通る
"""
import pytest
from fastapi.testclient import TestClient

from tests.test_api import auth
from tests.test_mail_threads import OWN, ins, msgid


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
        staff = db.create_user(conn, "tanaka")
        conn.execute("UPDATE users SET mail_role = ? WHERE id = ?", (db.MAIL_ROLE_STAFF, staff["id"]))
        admin = db.create_user(conn, "boss", is_admin=True)
        conn.execute("INSERT INTO mail_accounts (label, imap_host, imap_user, from_addr, created_at)"
                     " VALUES ('info', 'imap.example.com', ?, ?, '2026-01-01T00:00:00+00:00')", (OWN, OWN))
    return staff, admin


@pytest.fixture()
def conn(tmp_path, users):
    from server import db, mail_threads
    c = db.connect(tmp_path / "zatsumu.db")
    mail_threads.rebuild(c, mail_threads.Ctx(c))   # 空の DB を数え終えた (この後は少しずつ)
    yield c
    c.close()


def pend(conn):
    from server import mail_threads
    mail_threads.process_pending(conn)


def _thread(conn):
    """お客様1人の5通のスレッド (枝あり) と、ほかのお客様の1通。{名前: 番号}."""
    a = ins(conn, "taro@a.example.jp", subject="見積もり", status="replied")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a), status="archived")
    c = ins(conn, "taro@a.example.jp", irt=msgid(conn, a), status="replied", flagged="2026-10-01T00:00:00+00:00")
    d = ins(conn, "taro@a.example.jp", irt=msgid(conn, b), status="unhandled")
    e = ins(conn, "taro@a.example.jp", irt=msgid(conn, d), status="unhandled")
    x = ins(conn, "hanako@z.example.jp", status="unhandled")
    pend(conn)
    return dict(a=a, b=b, c=c, d=d, e=e, x=x)


def test_list_items_carry_thread_fields(client, users, conn):
    _, admin = users
    t = _thread(conn)
    r = client.get("/api/mail", headers=auth(admin))
    assert r.status_code == 200 and r.headers["X-Thread-Mode"] == "1"
    items = {m["id"]: m for m in r.json()}
    for k in "abcde":
        m = items[t[k]]
        assert m["th_key"] == t["a"] and m["th_size"] == 5 and m["th_open"] == 2
        assert m["th_flagged"] == 1 and m["th_newest_id"] == t["e"]
        assert m["th_first_at"] == items[t["a"]]["received_at"]
    x = items[t["x"]]
    assert (x["th_key"], x["th_size"], x["th_open"], x["th_flagged"], x["th_newest_id"]) == (t["x"], 1, 1, 0, t["x"])
    # 1通の詳細にもスレッドの印
    assert client.get(f"/api/mail/{t['b']}", headers=auth(admin)).json()["th_key"] == t["a"]
    assert client.get(f"/api/mail/{t['x']}", headers=auth(admin)).json()["th_key"] == t["x"]


def test_search_and_kill_switch_are_flat_and_counts_do_not_change(client, users, conn):
    _, admin = users
    t = _thread(conn)
    ins(conn, "low@l.example.jp", subject="お知らせ")
    conn.execute("UPDATE mails SET priority = 3 WHERE from_addr = 'low@l.example.jp'")
    conn.commit()

    def snap():
        out = {}
        for q in ("", "?hide_low=1", "?status=unhandled", "?q=見積もり", "?flagged=1"):
            r = client.get("/api/mail" + q, headers=auth(admin))
            out[q] = ([m["id"] for m in r.json()], r.headers.get("X-Hidden-Low"),
                      r.headers.get("X-Search-Total"), r.headers["X-Thread-Mode"])
        tabs = client.get("/api/mail/status", headers=auth(admin)).json()["tabs"]
        return out, tabs

    on, tabs_on = snap()
    assert on["?q=見積もり"][3] == "0" and on[""][3] == "1"
    r = client.get("/api/mail?q=見積もり", headers=auth(admin))
    assert all(m["th_size"] == 1 and m["th_key"] == m["id"] for m in r.json())
    # 設定でスレッドを止める (画面の切り替えも出さない)
    assert client.patch("/api/settings", headers=auth(admin), json={"mail_thread_mode": False}).status_code == 200
    assert client.get("/api/mail/status", headers=auth(admin)).json()["thread_mode"] is False
    assert client.get("/api/mail/me", headers=auth(admin)).json()["thread_mode"] is False
    off, tabs_off = snap()
    assert {k: v[:3] for k, v in on.items()} == {k: v[:3] for k, v in off.items()}
    assert tabs_on == tabs_off
    assert all(v[3] == "0" for v in off.values())
    r = client.get("/api/mail", headers=auth(admin))
    assert all(m["th_size"] == 1 and m["th_key"] == m["id"] for m in r.json())
    # 止めている間も、まとまりの記録は続ける (戻したらすぐまとまる)
    f = ins(conn, "taro@a.example.jp", irt=msgid(conn, t["e"]))
    pend(conn)
    assert client.patch("/api/settings", headers=auth(admin), json={"mail_thread_mode": True}).status_code == 200
    items = {m["id"]: m for m in client.get("/api/mail", headers=auth(admin)).json()}
    assert items[f]["th_key"] == t["a"] and items[f]["th_size"] == 6
    assert client.patch("/api/settings", headers=auth(admin), json={"mail_thread_mode": "たぶん"}).status_code == 422


def test_thread_members_in_tree_order(client, users, conn):
    staff, admin = users
    t = _thread(conn)
    for who in (admin, staff):
        r = client.get(f"/api/mail/threads/{t['a']}", headers=auth(who))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["key"] == t["a"] and d["total"] == 5 and d["shown"] == 5
        assert [(m["id"], m["depth"], m["parent_id"]) for m in d["items"]] == [
            (t["a"], 0, None), (t["b"], 1, t["a"]), (t["d"], 2, t["b"]), (t["e"], 3, t["d"]),
            (t["c"], 1, t["a"])]
        # 一覧と同じ項目 (状態・★・やり取りの数・取引先)
        m = d["items"][-1]
        assert m["status"] == "replied" and m["flagged"] is True and m["th_key"] == t["a"]
        assert "thread_count" in m and "client_name" in m
    # 1通だけのメールは、そのメールだけ
    d = client.get(f"/api/mail/threads/{t['x']}", headers=auth(admin)).json()
    assert d["total"] == 1 and [m["id"] for m in d["items"]] == [t["x"]] and d["items"][0]["depth"] == 0
    # スレッドの中のメールの番号 (印ではない) でも、そのスレッドを返す (まとめ直して印が変わった後)
    assert client.get(f"/api/mail/threads/{t['d']}", headers=auth(admin)).json()["key"] == t["a"]
    assert client.get("/api/mail/threads/99999", headers=auth(admin)).status_code == 404
    assert client.get(f"/api/mail/threads/{t['a']}").status_code == 401
    # /api/mail/{mail_id} に飲み込まれない
    assert client.get("/api/mail/threads/abc", headers=auth(admin)).status_code == 422


def test_thread_members_are_capped(client, users, conn, monkeypatch):
    from server import mail_threads
    _, admin = users
    monkeypatch.setattr(mail_threads, "MEMBERS_MAX", 5)
    prev = ins(conn, "taro@a.example.jp")
    ids = [prev]
    for _ in range(8):
        prev = ins(conn, "taro@a.example.jp", irt=msgid(conn, prev))
        ids.append(prev)
    pend(conn)
    d = client.get(f"/api/mail/threads/{ids[0]}", headers=auth(admin)).json()
    assert d["total"] == 9 and d["shown"] == 5
    # 最初のメールと、新しい方から4通
    assert [m["id"] for m in d["items"]] == [ids[0], *ids[-4:]]


def test_threads_check_is_admin_only(client, users, conn):
    staff, admin = users
    _thread(conn)
    r = client.get("/api/mail/threads-check", headers=auth(admin))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["threads"] == 1 and d["members"] == 5 and d["largest"]["size"] == 5
    assert d["pending"] == 0 and d["violations"] == [] and d["rule"] == d["signature"]
    assert set(d["v2_candidates"]) == {"count", "ids"}
    assert client.get("/api/mail/threads-check", headers=auth(staff)).status_code == 403
    assert client.get("/api/mail/threads-check").status_code == 401


def test_v2_candidates_show_same_subject_replies_without_headers(client, users, conn):
    _, admin = users
    ins(conn, "taro@a.example.jp", subject="清掃の見積もり")
    b = ins(conn, "taro@a.example.jp", subject="Re: 清掃の見積もり")
    ins(conn, "hanako@z.example.jp", subject="Re: 清掃の見積もり")   # 別の人 (前に届いていない)
    pend(conn)
    from server import mail_threads
    with pytest.MonkeyPatch.context() as mp:
        # テストのメールは 2026/9 に届いたことにしてあるので、最近の範囲を広げる
        mp.setattr(mail_threads, "V2_RECENT_DAYS", 3650)
        d = client.get("/api/mail/threads-check", headers=auth(admin)).json()
    assert d["v2_candidates"] == {"count": 1, "ids": [b]}


def test_tick_maintains_threads_even_when_auto_fetch_is_off(client, users, conn, tmp_path):
    from server import app as app_module, db, mail_threads
    a = ins(conn, "taro@a.example.jp")
    b = ins(conn, "taro@a.example.jp", irt=msgid(conn, a))
    with db.get_db(tmp_path / "zatsumu.db") as c:
        db.set_setting(c, "mail_fetch_enabled", 0)
    app_module._mail_tick()
    assert conn.execute("SELECT thread_id FROM mails WHERE id = ?", (b,)).fetchone()[0] == a
    assert mail_threads._checked is True


# ---------- 前の版の DB ----------

def test_migration_adds_column_table_index_and_triggers_once(tmp_path):
    from server import db
    path = tmp_path / "zatsumu.db"
    c = db.connect(path)
    c.execute("INSERT INTO mails (message_id, received_at, fetched_at) VALUES ('<a@x>', 'x', 'x')")
    # 前の版の形にする (トリガー・索引・表・列を消す)
    for name in ("trg_mails_thread_contact", "trg_mails_thread_replyid", "trg_sent_thread_ins",
                 "trg_sent_thread_link"):
        c.execute(f"DROP TRIGGER {name}")
    c.execute("DROP INDEX idx_mails_thread")
    c.execute("DROP TABLE mail_thread_todo")
    c.execute("ALTER TABLE mails DROP COLUMN thread_id")
    c.commit()
    c.close()
    c = db.connect(path)
    try:
        info = {r[1]: (r[2], r[3], r[4]) for r in c.execute("PRAGMA table_info(mails)")}
        assert info["thread_id"] == ("INTEGER", 0, None)
        assert c.execute("SELECT thread_id FROM mails").fetchone()[0] is None   # 書き足さない
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_mails_thread'").fetchone()[0]
        assert "WHERE thread_id IS NOT NULL" in sql and "(thread_id, status, flagged_at, received_at)" in sql
        trg = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'"
                                       " AND name LIKE '%thread%'")}
        assert trg == {"trg_mails_thread_contact", "trg_mails_thread_replyid", "trg_sent_thread_ins",
                       "trg_sent_thread_link"}
        fk = c.execute("PRAGMA foreign_key_list(mail_thread_todo)").fetchone()
        assert fk["table"] == "mails" and fk["on_delete"] == "CASCADE"
    finally:
        c.close()
    c = db.connect(path)
    try:
        assert c.total_changes == 0
    finally:
        c.close()


def test_previous_version_statements_still_work(tmp_path):
    """前の版のコードの書き方 (列を名前で並べた INSERT・相手の決め直し・送信箱の取り込みとつなぎ) は
    この版の DB でもそのまま通り、トリガーは作り直すメールを覚えるだけ."""
    from server import db, mail
    c = db.connect(tmp_path / "zatsumu.db")
    try:
        # 前の版の _insert_mails の INSERT (thread_id を知らない)
        c.execute(
            "INSERT OR IGNORE INTO mails (message_id, imap_uid, from_addr,"
            " from_name, to_addr, cc_addr, subject, body, received_at, fetched_at,"
            " priority, priority_reason, priority_source, category,"
            " assignee_user_id, draft_reply, draft_source, draft_generated_at,"
            " references_hdr, account_id, reply_to, contact_addr, contact_name,"
            " contact_source, contact_via_form, contact_company, list_mail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?)",
            ("<p@x>", 1, "taro@a.example.jp", "", OWN, "", "件名", "本文", "2026-09-01T00:00:00+00:00",
             "2026-09-01T00:00:00+00:00", 2, "", "rule", "", None, "", "", None, "", None, "",
             "taro@a.example.jp", "", "from", 0, "", 0))
        a = c.execute("SELECT id FROM mails WHERE message_id = '<p@x>'").fetchone()[0]
        mail.save_refs(c, a, "", "")
        c.execute("INSERT INTO mails (message_id, from_addr, subject, body, received_at, fetched_at,"
                  " contact_addr, contact_source) VALUES ('<q@x>', 'taro@a.example.jp', 's', 'b', 'x', 'x',"
                  " 'taro@a.example.jp', 'from')")
        b = c.execute("SELECT id FROM mails WHERE message_id = '<q@x>'").fetchone()[0]
        mail.save_refs(c, b, "<webmail@x>", "")
        c.execute("UPDATE mails SET thread_id = ? WHERE id IN (?, ?)", (a, a, b))   # まとまっていた
        c.commit()
        # 前の版の redecide_form_contacts の UPDATE (同じ値なら何もしない)
        c.executemany(
            "UPDATE mails SET contact_addr = ?, contact_name = ?, contact_source = ?,"
            " contact_via_form = ?, contact_company = ? WHERE id = ?",
            [("taro@a.example.jp", "", "from", 0, "", a)])
        assert c.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0
        c.executemany(
            "UPDATE mails SET contact_addr = ?, contact_name = ?, contact_source = ?,"
            " contact_via_form = ?, contact_company = ? WHERE id = ?",
            [("hanako@z.example.jp", "", "body", 1, "", a)])
        assert sorted(r[0] for r in c.execute("SELECT mail_id FROM mail_thread_todo")) == [a, b]
        assert [r[0] for r in c.execute("SELECT thread_id FROM mails ORDER BY id")] == [None, None]
        c.execute("DELETE FROM mail_thread_todo")
        # 前の版の fetch_sent_mail: 入れてから、どのメールへの返信かをつなぐ
        cur = c.execute(
            "INSERT OR IGNORE INTO sent_mails (account_id, message_id, imap_uid, folder,"
            " from_addr, from_name, to_addr, cc_addr, to_key, to_name, subject, body,"
            " sent_at, fetched_at, in_reply_to, references_hdr, source, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (None, "<webmail@x>", 3, "Sent", OWN, "", "taro@a.example.jp", "", "taro@a.example.jp", "",
             "Re: 件名", "本文", "2026-09-02T00:00:00+00:00", "2026-09-02T00:00:00+00:00", "<p@x>", "<p@x>",
             "webmail", ""))
        assert c.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0
        c.execute("UPDATE sent_mails SET linked_mail_id = ?, link_kind = ? WHERE id = ?",
                  (a, "header", cur.lastrowid))
        assert [r[0] for r in c.execute("SELECT mail_id FROM mail_thread_todo")] == [b]
        # 前の版の「返信の Message-ID」の書き方 (この画面から送った)
        c.execute("DELETE FROM mail_thread_todo")
        c.execute("UPDATE mails SET reply_message_id = ? WHERE id = ?", ("<webmail@x>", a))
        assert [r[0] for r in c.execute("SELECT mail_id FROM mail_thread_todo")] == [b]
        # 前の版の保存日数の片付け: 作り直すメールも一緒に消える
        c.executemany("DELETE FROM mail_refs WHERE mail_id = ?", [(b,)])
        c.executemany("DELETE FROM mails WHERE id = ?", [(b,)])
        assert c.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0] == 0
        c.commit()
    finally:
        c.close()
