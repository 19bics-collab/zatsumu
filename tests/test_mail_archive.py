"""ワンタップの「対応不要」と「元に戻す」のテスト.

- POST /api/mail/{id}/archive: 未対応 → 対応不要 (対応不要にした時刻・人・操作の記録)。
  見ている間に返信された・確認をお願いされた・もう対応不要のときは 409 で変えない
  (分かりやすい理由)。確認待ちのメールはここでは対応不要にしない
- POST /api/mail/{id}/unarchive: 対応不要にする前の状態 (未対応) に戻す。その後に返信された・
  確認をお願いされた・対応不要にし直されたときは 409 で戻さない
- メール担当者は 403 (どちらも)。勤怠だけの人・トークンなしも使えない
- PATCH の状態変更も、返信済み・確認待ちのメールは対応不要にしない
- 画面: 一覧の「対応不要」ボタン (管理者だけ)・「元に戻す」の知らせ・確かめの画面を出さない
- 対応不要は「返信しなくてよい」印: 受信箱は「状態：すべて」で開き、対応不要にした行も
  一覧に残って灰色の「対応不要」になる (「未対応」を選んでいるときだけ一覧から外れる)
  (一覧は500通までだが、未対応・確認待ちのメールは古くても出す)
- 一覧・詳細に、対応不要にした時刻・人と、返信した人・どこから返信したか (Webメール・Gmail) が出る
"""
import re

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth


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


def _archive(client, user, mid, from_status="unhandled"):
    return client.post(f"/api/mail/{mid}/archive", headers=auth(user),
                       json={"from_status": from_status})


def _undo(client, user, mid, r):
    return client.post(f"/api/mail/{mid}/unarchive", headers=auth(user),
                       json={"restore_to": r["previous_status"], "archived_at": r["archived_at"]})


def test_archive_then_undo_restores_unhandled(client, users, tmp_path):
    _, admin, _ = users
    mid = _add_mail(tmp_path, subject="ご案内（返事不要）")
    other = _add_mail(tmp_path, n=2)
    r = _archive(client, admin, mid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "archived" and out["previous_status"] == "unhandled"
    assert out["subject"] == "ご案内（返事不要）"
    row = _row(tmp_path, mid)
    assert row["status"] == "archived" and row["archived_at"] == out["archived_at"]
    # 「未対応」の一覧から外れ、タブと未対応の数も減る (「すべて」の一覧には残る。次のテスト)
    ids = [m["id"] for m in client.get("/api/mail?status=unhandled", headers=auth(admin)).json()]
    assert ids == [other]
    st = client.get("/api/mail/status?status=unhandled", headers=auth(admin)).json()
    assert st["unhandled"] == 1 and st["tabs"]["all"] == 1
    a = _audits(tmp_path, "mail_archive")
    assert len(a) == 1 and a[0]["admin_id"] == admin["id"]
    assert a[0]["detail"] == f"mail={mid} status=unhandled->archived"

    # 元に戻す: 未対応に戻り、対応不要にした時刻も消える (週ごとのまとめの数に入らない)
    r = _undo(client, admin, mid, out)
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": mid, "status": "unhandled"}
    row = _row(tmp_path, mid)
    assert row["status"] == "unhandled" and row["archived_at"] is None
    st = client.get("/api/mail/status", headers=auth(admin)).json()
    assert st["unhandled"] == 2
    a = _audits(tmp_path, "mail_unarchive")
    assert len(a) == 1 and a[0]["detail"] == f"mail={mid} status=archived->unhandled"
    # もう一度「元に戻す」を押しても (もう未対応)、何もしない
    r = _undo(client, admin, mid, out)
    assert r.status_code == 409 and "もう未対応に戻っています" in r.json()["detail"]
    # 操作の記録 (CSV) には分かりやすい名前で出る
    csv = client.get("/api/reports/audit.csv", headers=auth(admin)).text
    assert "メールを対応不要にした" in csv and "メールの対応不要を元に戻した" in csv


def test_archived_mail_stays_in_default_list(client, users, tmp_path):
    """受信箱を開いたときの一覧 (状態を送らない =「すべて」) には、対応不要にしたメールも残る."""
    _, admin, _ = users
    mid = _add_mail(tmp_path, subject="ご案内（返事不要）")
    other = _add_mail(tmp_path, n=2)
    out = _archive(client, admin, mid).json()

    def listed():
        r = client.get("/api/mail?sort=date&hide_low=1", headers=auth(admin))
        return [(m["id"], m["status"]) for m in r.json()]

    assert listed() == [(other, "unhandled"), (mid, "archived")]
    # 未対応の数 (受信箱のボタン・タブの名前) とタブの数 (「すべて」のとき) からは外れる
    st = client.get("/api/mail/status?hide_low=1", headers=auth(admin)).json()
    assert st["unhandled"] == 1 and st["tabs"]["all"] == 1
    # 「元に戻す」で、同じ場所のまま未対応に戻る
    assert _undo(client, admin, mid, out).status_code == 200
    assert listed() == [(other, "unhandled"), (mid, "unhandled")]
    st = client.get("/api/mail/status?hide_low=1", headers=auth(admin)).json()
    assert st["unhandled"] == 2 and st["tabs"]["all"] == 2


def test_default_list_keeps_old_open_mails_beyond_limit(client, users, tmp_path):
    """「状態：すべて」の一覧は500通までだが、古い未対応・確認待ちのメールも必ず出す.

    新しい順の500通だけだと、後に届いた返信済み・対応不要のメールが500通を超えたときに、
    その前に届いた未対応のメールが一覧に出ず、未対応の数 (1) とも合わなくなっていた。
    """
    from server import app as app_module
    from server import db
    _, admin, _ = users
    limit = app_module.MAIL_LIST_LIMIT
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        def add(key, subject, when, priority, status):
            return conn.execute(
                "INSERT INTO mails (message_id, from_addr, from_name, subject, body, received_at,"
                " fetched_at, priority, status) VALUES (?, ?, 'A', ?, '本文', ?, ?, ?, ?)",
                (f"<{key}@example.com>", f"{key}@example.com", subject, when, when,
                 priority, status)).lastrowid
        old = add("old", "古い未対応", "2026-05-01T00:00:00+00:00", 1, "unhandled")
        pending = add("pend", "古い確認待ち", "2026-05-02T00:00:00+00:00", 2, "pending_approval")
        old_done = add("done", "古い返信済み", "2026-05-03T00:00:00+00:00", 2, "replied")
        for i in range(limit):
            t = f"2026-06-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00"
            add(f"r{i}", f"お知らせ{i}", t, 2, "replied" if i % 2 else "archived")

    def listed(query):
        r = client.get("/api/mail?" + query, headers=auth(admin))
        assert r.status_code == 200, r.text
        return [m["id"] for m in r.json()], r.headers

    for sort in ("date", "priority"):
        ids, _ = listed(f"sort={sort}&hide_low=1")
        assert len(ids) == limit and old in ids and pending in ids, sort
        assert old_done not in ids, sort   # 返信済み・対応不要は新しい方だけ
    # 並びはいつもの順 (新しい順なら、古い2通は一番下)
    ids, _ = listed("sort=date&hide_low=1")
    assert ids[-2:] == [pending, old]
    # 一覧と、未対応の数・タブの数 (未対応と確認待ち) が合う
    st = client.get("/api/mail/status?hide_low=1", headers=auth(admin)).json()
    assert st["unhandled"] == 1 and st["tabs"]["all"] == 2
    # 「状態」を選んだときは今までどおり
    assert listed("status=unhandled&sort=date&hide_low=1")[0] == [old]
    # 探したときは今までどおり新しい方から500通 (合った数はヘッダに出し、画面が「新しい方から」と出す)
    ids, headers = listed("q=" + "本文")
    assert len(ids) == limit and old not in ids and pending not in ids
    assert headers["X-Search-Total"] == str(limit + 3)


def test_archive_refuses_when_status_changed(client, users, tmp_path):
    """見ている間にほかの人が返信した・確認をお願いした・対応不要にしたメールは変えない."""
    _, admin, _ = users
    replied = _add_mail(tmp_path, n=1, status="replied")
    pending = _add_mail(tmp_path, n=2, status="pending_approval")
    archived = _add_mail(tmp_path, n=3, status="archived")
    r = _archive(client, admin, replied)
    assert r.status_code == 409 and "もう返信してあります" in r.json()["detail"]
    r = _archive(client, admin, pending)
    assert r.status_code == 409 and "確認待ち" in r.json()["detail"]
    r = _archive(client, admin, archived)
    assert r.status_code == 409 and "もう対応不要" in r.json()["detail"]
    assert [_row(tmp_path, m)["status"] for m in (replied, pending, archived)] == [
        "replied", "pending_approval", "archived"]
    assert _row(tmp_path, replied)["archived_at"] is None
    assert _audits(tmp_path, "mail_archive") == []
    # 未対応のほかは、画面から頼まれても対応不要にしない (確認待ちの見落としを防ぐ)
    r = _archive(client, admin, pending, from_status="pending_approval")
    assert r.status_code == 400
    assert _row(tmp_path, pending)["status"] == "pending_approval"
    # 無いメール・おかしな値
    assert _archive(client, admin, 9999).status_code == 404
    assert client.post(f"/api/mail/{replied}/archive", headers=auth(admin),
                       json={}).status_code == 422


def test_archive_checks_status_again_when_writing(client, users, tmp_path, monkeypatch):
    """確かめた直後 (書き込む前) に返信されたときも、対応不要にしない."""
    from server import app as app_module
    _, admin, _ = users
    mid = _add_mail(tmp_path)
    real = app_module._mail_or_404
    calls = []

    def racy(conn, mail_id):
        row = real(conn, mail_id)
        if not calls:
            # 1回目に読んだ後で、ほかの人が返信を送った
            conn.execute("UPDATE mails SET status = 'replied' WHERE id = ?", (mail_id,))
        calls.append(mail_id)
        return row

    monkeypatch.setattr(app_module, "_mail_or_404", racy)
    r = _archive(client, admin, mid)
    assert r.status_code == 409 and "もう返信してあります" in r.json()["detail"]
    assert _audits(tmp_path, "mail_archive") == []


def test_undo_refuses_when_mail_changed_meanwhile(client, users, tmp_path):
    _, admin, staff = users
    # 対応不要にした後で、ほかの人が返信した
    a = _add_mail(tmp_path, n=1)
    ra = _archive(client, admin, a).json()
    _set(tmp_path, a, status="replied")
    r = _undo(client, admin, a, ra)
    assert r.status_code == 409 and "ほかの人が返信しました" in r.json()["detail"]
    assert _row(tmp_path, a)["status"] == "replied"
    # 対応不要にした後で、メール担当者が確認をお願いした (対応不要のメールからもお願いできる)
    b = _add_mail(tmp_path, n=2)
    rb = _archive(client, admin, b).json()
    r = client.post(f"/api/mail/{b}/request-approval", headers=auth(staff),
                    json={"body": "山田 様\n\nありがとうございます。"})
    assert r.status_code == 200, r.text
    r = _undo(client, admin, b, rb)
    assert r.status_code == 409 and "確認待ち" in r.json()["detail"]
    assert _row(tmp_path, b)["status"] == "pending_approval"
    # 対応不要にした後で、ほかの人が未対応に戻して、また対応不要にした (別の操作の「元に戻す」はしない)
    c = _add_mail(tmp_path, n=3)
    rc = _archive(client, admin, c).json()
    _set(tmp_path, c, status="unhandled")
    rc2 = _archive(client, admin, c).json()
    assert rc2["archived_at"] != rc["archived_at"]
    r = _undo(client, admin, c, rc)
    assert r.status_code == 409 and "ほかの操作" in r.json()["detail"]
    assert _row(tmp_path, c)["status"] == "archived"
    # 新しい方の「元に戻す」は効く
    assert _undo(client, admin, c, rc2).status_code == 200
    # 戻す先は未対応だけ (返信済みなどに「戻す」ことはできない)
    d = _add_mail(tmp_path, n=4)
    rd = _archive(client, admin, d).json()
    r = _undo(client, admin, d, {**rd, "previous_status": "replied"})
    assert r.status_code == 400
    assert _row(tmp_path, d)["status"] == "archived"
    assert len(_audits(tmp_path, "mail_unarchive")) == 1


def test_staff_and_members_cannot_archive_or_undo(client, users, tmp_path):
    worker, admin, staff = users
    mid = _add_mail(tmp_path)
    r = _archive(client, staff, mid)
    assert r.status_code == 403 and "管理者だけ" in r.json()["detail"]
    assert _row(tmp_path, mid)["status"] == "unhandled"
    assert _archive(client, worker, mid).status_code == 403
    assert client.post(f"/api/mail/{mid}/archive",
                       json={"from_status": "unhandled"}).status_code == 401
    out = _archive(client, admin, mid).json()
    r = _undo(client, staff, mid, out)
    assert r.status_code == 403 and "管理者だけ" in r.json()["detail"]
    assert _undo(client, worker, mid, out).status_code == 403
    assert _row(tmp_path, mid)["status"] == "archived"
    assert _audits(tmp_path, "mail_unarchive") == []


def test_patch_does_not_archive_replied_or_pending_mail(client, users, tmp_path):
    """返信画面の古い版 (PATCH で状態を変える) からも、返信済み・確認待ちは対応不要にしない."""
    _, admin, _ = users
    replied = _add_mail(tmp_path, n=1, status="replied")
    pending = _add_mail(tmp_path, n=2, status="pending_approval")
    for mid, word in ((replied, "もう返信してあります"), (pending, "確認待ち")):
        r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                         json={"status": "archived", "priority": 1})
        assert r.status_code == 409 and word in r.json()["detail"]
        row = _row(tmp_path, mid)
        assert row["status"] != "archived" and row["priority"] == 2   # ほかの変更もしない
    # 未対応のメールは今までどおり PATCH でも対応不要にできる
    mid = _add_mail(tmp_path, n=3)
    assert client.patch(f"/api/mail/{mid}", headers=auth(admin),
                        json={"status": "archived"}).status_code == 200
    assert _row(tmp_path, mid)["status"] == "archived"


def test_archive_records_who_and_undo_clears_it(client, users, tmp_path):
    """対応不要にした人を残し (一覧の「✓ 対応不要」の横に出す)、元に戻したら消す."""
    _, admin, _ = users
    mid = _add_mail(tmp_path, n=1)
    out = _archive(client, admin, mid).json()
    assert out["archived_by"] == "boss"
    assert _row(tmp_path, mid)["archived_by"] == "boss"
    item = next(m for m in client.get("/api/mail", headers=auth(admin)).json() if m["id"] == mid)
    assert item["archived_by"] == "boss" and item["archived_at"] == out["archived_at"]
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["archived_by"] == "boss" and d["archived_at"] == out["archived_at"]
    assert _undo(client, admin, mid, out).status_code == 200
    row = _row(tmp_path, mid)
    assert row["archived_by"] == "" and row["archived_at"] is None
    item = next(m for m in client.get("/api/mail", headers=auth(admin)).json() if m["id"] == mid)
    assert item["archived_by"] == "" and item["archived_at"] is None
    # PATCH で対応不要にしたときも残す
    other = _add_mail(tmp_path, n=2)
    assert client.patch(f"/api/mail/{other}", headers=auth(admin),
                        json={"status": "archived"}).status_code == 200
    row = _row(tmp_path, other)
    assert row["archived_by"] == "boss" and row["archived_at"]
    # 未対応に戻した後は、一覧には出さない (いまは対応不要ではないので)
    assert client.patch(f"/api/mail/{other}", headers=auth(admin),
                        json={"status": "unhandled"}).status_code == 200
    item = next(m for m in client.get("/api/mail", headers=auth(admin)).json() if m["id"] == other)
    assert item["archived_by"] == "" and item["archived_at"] is None
    # 前の版が対応不要にしたメール (人が空) は、時刻だけ
    old = _add_mail(tmp_path, n=3, status="archived")
    _set(tmp_path, old, archived_at="2026-10-01T00:00:00+00:00")
    item = next(m for m in client.get("/api/mail", headers=auth(admin)).json() if m["id"] == old)
    assert item["archived_by"] == "" and item["archived_at"] == "2026-10-01T00:00:00+00:00"


def test_summary_tells_who_replied_and_from_where(client, users, tmp_path):
    """一覧の「↩ 返信済み」の横に出す、誰が (この画面) / どこから (Webメール・Gmail) 返信したか."""
    _, admin, _ = users
    app_sent = _add_mail(tmp_path, n=1, status="replied")
    webmail = _add_mail(tmp_path, n=2, status="replied")
    gmail = _add_mail(tmp_path, n=3, status="replied")
    at = "2026-10-29T05:03:00+00:00"
    _set(tmp_path, app_sent, replied_at=at, replied_by="boss")
    _set(tmp_path, webmail, replied_at=at, replied_by="Webメール", reply_source="webmail")
    _set(tmp_path, gmail, replied_at=at, replied_by="Gmail", reply_source="gmail")
    # 返信していないメールに、前の値が残っていても出さない
    open_mail = _add_mail(tmp_path, n=4)
    _set(tmp_path, open_mail, replied_by="boss", reply_source="webmail")
    items = {m["id"]: m for m in client.get("/api/mail", headers=auth(admin)).json()}
    assert (items[app_sent]["replied_by"], items[app_sent]["reply_source"]) == ("boss", "")
    assert (items[webmail]["replied_by"], items[webmail]["reply_source"]) == ("Webメール", "webmail")
    assert (items[gmail]["replied_by"], items[gmail]["reply_source"]) == ("Gmail", "gmail")
    assert items[app_sent]["replied_at"] == at
    assert (items[open_mail]["replied_by"], items[open_mail]["reply_source"]) == ("", "")
    # 詳細も同じ値 (返信画面の「〜から送信」)
    d = client.get(f"/api/mail/{webmail}", headers=auth(admin)).json()
    assert d["replied_by"] == "Webメール" and d["reply_source"] == "webmail"
    assert d["reply_source_label"] == "Webメールなど"


def test_patch_status_checks_again_when_writing(client, users, tmp_path, monkeypatch):
    """PATCH で確かめた直後に返信されたときも、状態を書きかえない (ほかの変更もしない)."""
    from server import app as app_module
    _, admin, _ = users
    mid = _add_mail(tmp_path)
    real = app_module._mail_or_404

    def racy(conn, mail_id):
        row = real(conn, mail_id)
        conn.execute("UPDATE mails SET status = 'replied' WHERE id = ?", (mail_id,))
        conn.commit()   # ほかの人の送信は、もう確定している
        return row

    monkeypatch.setattr(app_module, "_mail_or_404", racy)
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"status": "archived", "priority": 1})
    assert r.status_code == 409 and "ほかの操作で変わりました" in r.json()["detail"]
    row = _row(tmp_path, mid)
    assert row["status"] == "replied" and row["priority"] == 2


def test_mail_page_has_one_tap_archive_and_undo(client):
    page = client.get("/mail").text
    # 一覧の「対応不要」ボタン (管理者だけ・未対応の行だけ) と、その列
    assert 'id="th-act"' in page and 'class="row-archive"' in page
    # 列は全員に出す (「本文 ▼」のボタンも入る)。「対応不要」は管理者の未対応の行だけ
    assert '$("th-act").hidden' not in page
    assert 'const canArchive = admin && m.status === "unhandled";' in page
    assert '${canArchive ? archiveButton(m) : ""}' in page
    # ボタンを押しても返信画面は開かない
    assert 'e.target.closest("button.row-archive")' in page and "e.stopPropagation()" in page
    # 新しい API を使い、「元に戻す」で前の状態と時刻を送り返す
    assert "/archive`" in page and "/unarchive`" in page
    assert "restore_to: it.r.previous_status, archived_at: it.r.archived_at" in page
    # 画面の下の知らせ (6秒ほど・重ねる)
    assert 'id="toasts"' in page and "TOAST_MS = 6000" in page
    assert "対応不要にしました（" in page and '"元に戻す"' in page
    # 返信画面の「対応不要にする」も、確かめの画面を出さずに閉じて知らせを出す
    m = re.search(r'\$\("m-archive"\)\.onclick = async \(\) => \{(.*?)\n\};', page, re.S)
    assert m and "confirm(" not in m.group(1)
    assert "closeModal()" in m.group(1) and "showArchivedToast" in m.group(1)
    a = re.search(r"async function archiveFromRow\(b, tr\) \{(.*?)\n\}", page, re.S)
    assert a and "confirm(" not in a.group(1)
    # スマホではカードの右上に置き、件名などを狭めない
    assert ".table-wrap tr.mail-row td.act { position: absolute;" in page
    # 外から来た件名は textContent で入れる
    assert "text.textContent = `対応不要にしました（" in page


def test_mail_page_puts_archive_button_at_top_of_reply_screen(client):
    """返信画面の「対応不要にする」は件名の上の列 (本文を全部出すので、下まで進まなくても押せる)."""
    page = client.get("/mail").text
    modal = page[page.index('<div id="mail-modal"'):page.index('<div id="att-viewer"')]
    top = modal[modal.index('<div class="m-top">'):modal.index('<h2 id="m-subject"')]
    # 上の列に「前のメールに戻る」「対応不要にする」「閉じる」が並び、件名より前にある
    for marker in ('id="m-back"', 'id="m-archive"', 'id="m-close-top"'):
        assert marker in top, marker
    assert modal.count('id="m-archive"') == 1
    # 下の列にも「閉じる」は残す (返信を送った後にそのまま閉じられる)
    bar = modal[modal.index('<div class="bar"'):]
    assert 'id="m-close"' in bar and 'id="m-archive"' not in bar
    assert '$("m-close").onclick = $("m-close-top").onclick = () => closeModal();' in page
    # 指で押しやすい高さ。赤 (消す操作) ではなく、「閉じる」(灰色) とも違う見た目
    assert ".m-top button { min-height: 44px;" in page
    css = page[page.index("  .m-archive {"):page.index("\n", page.index("  .m-archive {"))]
    assert "border:" in css and "#b91c1c" not in css and "#c62828" not in css
    assert 'class="m-archive"' in top and 'id="m-archive" class="ghost"' not in page
    # 入りきらないときは、右の2つをまとめて次の行に回す
    assert '<div class="m-top-act">' in top and ".m-top { display: flex;" in page
    assert "flex-wrap: wrap;" in page[page.index(".m-top {"):page.index("\n", page.index(".m-top {"))]
    # iPhone の縦では「← 戻る」と短くする (読み上げでは「前のメールに戻る」)
    assert 'aria-label="前のメールに戻る"' in top and '<span class="m-back-long">前のメールに</span>戻る' in top
    assert "@media (max-width: 480px) { .m-back-long { display: none; } }" in page


def test_mail_page_guards_against_accidental_second_tap(client):
    """行が消えて下の行が上がってきた直後は、同じ所をもう一度押しても次のメールを対応不要にしない."""
    page = client.get("/mail").text
    assert "const ROW_QUIET_MS = 600;" in page
    # 行を消したとき・自動で取り直して行が動いたときに時刻を覚え、その間のボタンは何もしない
    assert "removeRows([tr, pk], next && next.classList.contains(\"mail-row\") ? next : null);\n    rowsMovedAt = Date.now();" in page
    assert "rowsMovedAt = Date.now();" in page.split("function renderInbox(")[1].split("tbody.innerHTML")[0]
    m = re.search(r'tr\.onclick = e => \{(.*?)\n    \}\);', page, re.S)
    assert m and "Date.now() - rowsMovedAt < ROW_QUIET_MS) return;" in m.group(1)
    assert m.group(1).index("ROW_QUIET_MS") < m.group(1).index("archiveFromRow(b, tr)")


def test_mail_page_keeps_archive_button_visible_when_table_is_wide(client):
    """iPad の縦・iPhone の横向きなどで表が横にはみ出しても、ボタンの列は右端に出したまま."""
    page = client.get("/mail").text
    assert "#mail-table th#th-act, .mail-row td.act { position: sticky; right: 0;" in page
    # 地の色は行と同じ (新着・マウスを乗せたとき)
    assert ".mail-row.new td.act { background: #fefce8; }" in page
    assert ".mail-row:hover td.act { background: #f0fdf4; }" in page
    # スマホのカードでは今までどおり右上 (地の色は付けない)
    assert "padding: 0; border: none; background: transparent;" in page


def test_mail_page_merges_undo_notices_instead_of_dropping(client):
    """続けて4通目を対応不要にしても、古い知らせは消さずにまとめ、全部元に戻せる."""
    page = client.get("/mail").text
    assert "box.firstElementChild.remove()" not in page
    m = re.search(r"function makeRoomForToast\(box\) \{(.*?)\n\}", page, re.S)
    assert m and "idle[1].undo.add(idle[0].undo.items)" in m.group(1)
    # 消してよいのは、戻し終わった知らせ (ボタンのないもの) だけ
    assert "t.undo.done" in m.group(1)
    assert "通とも元に戻す" in page and "for (const it of ctl.items)" in page
    # 戻せなかった理由は最後に1回だけ出す
    assert "次のメールは戻せませんでした。" in page


def test_mail_page_keeps_undo_while_mail_is_open(client):
    """返信画面を開いている間は知らせの時間を数えず、閉じたら数秒出し直す."""
    page = client.get("/mail").text
    assert "TOAST_AGAIN_MS = 4000" in page
    assert 'if (!ctl.busy && !anyModalOpen()) close();' in page
    assert "if (modalWasOpen && !open) for (const t of $(\"toasts\").children) t.undo?.again();" in page
    assert "Math.max(ctl.until - Date.now(), TOAST_AGAIN_MS)" in page


def test_mail_page_inbox_opens_with_all_statuses(client):
    """受信箱は「状態：すべて」で開く (返信済み・対応不要のメールも一覧に残す)。選んだ状態は覚えない."""
    page = client.get("/mail").text
    start = page.index('<select id="filter"')
    sel = page[start:page.index("</select>", start)]
    # 先頭 (開いたときに選ばれているもの) が「すべて」。未対応などは選べば今までどおり絞り込む
    assert re.findall(r'<option value="([^"]*)"', sel) == [
        "", "unhandled", "pending_approval", "replied", "archived"]
    assert '<option value="">状態：すべて</option>' in sel and "selected" not in sel
    # ブラウザが再読み込みで前の選択に戻さない。このブラウザに覚えておくこともしない
    assert 'autocomplete="off"' in sel[:sel.index(">")]
    assert not re.search(r"(savePref|loadPref|localStorage\.\w+)\([^)]*filter\b", page)
    # 「状態」で選んでいて何も出ないときは、「すべて」に戻すと出ることを案内する
    assert 'const filtered = $("filter").value || activeTab !== TAB_ALL' in page
    # 使い方: 対応不要にしても一覧に残る (返信しなくてよい印)。未対応だけを見る方法も書く
    howto = page[page.index('data-howto="inbox"'):]
    howto = howto[:howto.index("</details>")]
    for text in ("返信済み・対応不要にしたメールも一覧に残り、灰色で出ます",
                 "上の「状態」で「未対応」を選んでください",
                 "対応不要にしたメールは一覧に残り、灰色で「対応不要」と出ます",
                 "（返信しなくてよい印です）"):
        assert text in howto, text
    assert "「対応不要」を押すとすぐ一覧から外れます" not in page


def test_mail_page_archive_keeps_row_in_list(client):
    """対応不要 (行・プルダウン・返信画面) にしても、行は一覧に残り、その場で灰色の「対応不要」になる."""
    page = client.get("/mail").text
    a = re.search(r"async function archiveFromRow\(b, tr\) \{(.*?)\n\}", page, re.S).group(1)
    # 行を動かして消すのは「状態」で「未対応」を選んでいるときだけ。ほかは行を残して灰色にする
    assert 'const leave = $("filter").value === "unhandled";' in a
    assert "} else if (!leave) {\n    showRowArchived(id, r);" in a
    assert a.index("showRowArchived(id, r)") < a.index("showArchivedToast(") < a.rindex("refreshInbox();")
    s = re.search(r"function showRowArchived\(id, r = \{\}\) \{(.*?)\n\}", page, re.S).group(1)
    assert 'const done = { status: "archived", archived_at: r.archived_at || null,' in s
    assert "if (m) Object.assign(m, done);" in s
    assert 'tr.classList.add("done");' in s and 'tr.classList.remove("two-btn");' in s
    # 「対応不要」のボタンは行とプルダウンの両方から消す。状態の欄は取り直したときと同じ印
    # (「✓ 対応不要」と、対応不要にした時刻・人。statusMark が esc() を通す)
    assert ('document.querySelectorAll(`button.row-archive[data-archive="${id}"]`)'
            '.forEach(x => x.remove());') in s
    assert '.innerHTML = statusMark({ ...(m || {}), ...done });' in s
    # ボタンが消えて行の高さが変わっても、見ている所は動かさない (一番上に見えている行の位置をそろえる)
    assert ".find(x => x.getBoundingClientRect().bottom > 0)" in s
    assert (s.index("const anchorTop = ") < s.index(".forEach(x => x.remove());")
            < s.index("window.scrollBy(0, anchor.getBoundingClientRect().top - anchorTop);"))
    # ボタンを指すと、返信しなくてよい印だと分かる
    assert 'title="返信しなくてよい印を付けます">対応不要</button>' in page
    assert 'title="返信しなくてよい印を付けます（メールは受信箱に残ります）">✓ 対応不要にする</button>' in page
    # 取り直した後も同じ見た目 (灰色の行・「対応不要」のボタンは未対応の行だけ)
    assert '["unhandled", "pending_approval"].includes(m.status) ? "" : " done"' in page
    assert ".mail-row.done td { color: #9ca3af; }" in page
    assert 'const canArchive = admin && m.status === "unhandled";' in page
    # 返信画面の「対応不要にする」も、閉じた後に行を残して灰色にする
    m = re.search(r'\$\("m-archive"\)\.onclick = async \(\) => \{(.*?)\n\};', page, re.S).group(1)
    assert 'if ($("filter").value !== "unhandled") showRowArchived(id, r);' in m
    assert m.index("await closeModal()") < m.index("showRowArchived(id, r)")


def test_mail_page_keeps_keyboard_focus_after_archive(client):
    """キーボードで「対応不要」を押しても・一覧を描き直しても、フォーカスが先頭に飛ばない (画面も動かさない)."""
    page = client.get("/mail").text
    a = re.search(r"async function archiveFromRow\(b, tr\) \{(.*?)\n\}", page, re.S).group(1)
    # ボタンを押せなくする前に、どこにいたかを覚える
    assert "const focus = document.activeElement === b ? focusedRowButton(tr.parentNode) : null;" in a
    assert a.index("focusedRowButton(") < a.index("x.disabled = true")
    assert "showRowArchived(id, r);\n    refocus(focus);" in a
    # 消したボタンの代わりは、行の「本文 ▼」かプルダウンの「閉じる」
    f = page[page.index("function focusedRowButton("):page.index("function peekButton(")]
    assert "button[data-pk-close]" in f and "button.row-peek" in f
    assert "b?.focus({ preventScroll: true });" in f and "!x.disabled" in f
    # 一覧・プルダウンを描き直しても、同じボタンに戻す
    r = page[page.index("function renderInbox("):page.index('$("filter").onchange')]
    assert r.index("const focus = focusedRowButton(tbody);") < r.index("tbody.innerHTML")
    assert (r.index("window.scrollBy(0, again.getBoundingClientRect().top - anchorTop);")
            < r.index("refocus(focus);"))
    fp = page[page.index("function fillPeek("):page.index("async function loadPeek(")]
    assert "const focus = focusedRowButton(box);" in fp and "refocus(focus);" in fp


def test_mail_page_ignores_second_tap_after_archive_keeps_row(client):
    """対応不要にして行を残したとき (「状態：すべて」) も、すぐ同じ所を押したら何もしない.

    行の「対応不要」が消えて、その場所に「本文 ▼」・行 (ボタンが縦に並ぶ幅)・プルダウンの「閉じる」が
    来るので、2回押したつもりの2回目で、対応不要にしたばかりのメールのプルダウン・返信画面を開いていた。
    """
    page = client.get("/mail").text
    # ボタンを消したら、行が動いたときと同じく時刻を覚える
    s = re.search(r"function showRowArchived\(id, r = \{\}\) \{(.*?)\n\}", page, re.S).group(1)
    assert s.index(".forEach(x => x.remove());") < s.index("rowsMovedAt = Date.now();")
    # 行の中は「対応不要」だけでなく、「本文 ▼」・行 (返信画面を開く) も、その間は何もしない
    m = re.search(r'tr\.onclick = e => \{(.*?)\n    \}\);', page, re.S).group(1)
    quiet = m.index("if (Date.now() - rowsMovedAt < ROW_QUIET_MS) return;")
    assert m.index("if (f || p || b) e.stopPropagation();") < quiet
    for call in ("toggleFlag(", "togglePeek(", "archiveFromRow(b, tr)", "openMail("):
        assert quiet < m.index(call), call
    # プルダウンの中のボタン (「閉じる」など) も全部
    click = page[page.index('document.querySelector("#mail-table tbody").addEventListener("click"'):]
    click = click[:click.index("\n});")]
    quiet = click.index("if (Date.now() - rowsMovedAt < ROW_QUIET_MS) return;")
    for key in ("d.pkOpen", "d.pkClose", "d.pkEarlier", "d.pkRetry", 'b.matches("button.row-archive")'):
        assert quiet < click.index(key), key


def test_mail_page_archive_clears_new_mark(client):
    """対応不要にしたメールは見たことにして、「新着」の印 (黄色の行) を外す (描き直しても付けない)."""
    page = client.get("/mail").text
    a = re.search(r"async function archiveFromRow\(b, tr\) \{(.*?)\n\}", page, re.S).group(1)
    # 対応不要にできたときだけ (できなかったときは前で戻る)。行を残すときも、消すときも
    assert a.index("if (!r) {") < a.index("markOpened(id);") < a.index("if (leave && tr.isConnected)")
    mo = page[page.index("function markOpened(id) {"):]
    mo = mo[:mo.index("\n}")]
    assert "openedIds.add(Number(id));" in mo and 'row.classList.remove("new");' in mo
    assert "const isNew = id > seenId && !openedIds.has(id);" in page
