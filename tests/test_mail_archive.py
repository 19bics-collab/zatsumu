"""ワンタップの「対応不要」と「元に戻す」のテスト.

- POST /api/mail/{id}/archive: 未対応 → 対応不要 (対応不要にした時刻・操作の記録)。
  見ている間に返信された・確認をお願いされた・もう対応不要のときは 409 で変えない
  (分かりやすい理由)。確認待ちのメールはここでは対応不要にしない
- POST /api/mail/{id}/unarchive: 対応不要にする前の状態 (未対応) に戻す。その後に返信された・
  確認をお願いされた・対応不要にし直されたときは 409 で戻さない
- メール担当者は 403 (どちらも)。勤怠だけの人・トークンなしも使えない
- PATCH の状態変更も、返信済み・確認待ちのメールは対応不要にしない
- 画面: 一覧の「対応不要」ボタン (管理者だけ)・「元に戻す」の知らせ・確かめの画面を出さない
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
    # 受信箱 (未対応) から外れ、タブと未対応の数も減る
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
    assert '$("th-act").hidden = !admin' in page
    assert 'm.status === "unhandled"' in page and "${admin ?" in page
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


def test_mail_page_guards_against_accidental_second_tap(client):
    """行が消えて下の行が上がってきた直後は、同じ所をもう一度押しても次のメールを対応不要にしない."""
    page = client.get("/mail").text
    assert "const ROW_QUIET_MS = 600;" in page
    # 行を消したとき・自動で取り直して行が動いたときに時刻を覚え、その間のボタンは何もしない
    assert "tr.remove(); rowsMovedAt = Date.now();" in page
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
