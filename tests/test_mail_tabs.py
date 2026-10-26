"""受信箱のタブ (種類ごと・重要（高）) と並び順 (新しい順・重要度順) のテスト.

- GET /api/mail の sort (date=新しい順が既定 / priority=重要度順。ほかの値は 400)
- 未来の日付を付けたメール (迷惑メールなど) が、新しい順の一番上に居座らないこと
- 新しい順で500通を超えるときは、新しい方の500通を出すこと
- GET /api/mail/status の tabs (種類ごと・重要・未分類の、未対応と確認待ちの数) と権限
- 画面にタブと並び順の切り替えがあること
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth


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
    """勤怠だけの人・管理者・メール担当者."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
        staff = db.create_user(conn, "suzuki")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
    return worker, admin, staff


def _set(tmp_path, mid, **cols):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE mails SET {sets} WHERE id = ?", (*cols.values(), mid))


def _ids(client, user, query=""):
    r = client.get("/api/mail" + query, headers=auth(user))
    assert r.status_code == 200, r.text
    return [m["id"] for m in r.json()]


def _tabs(client, user, query=""):
    r = client.get("/api/mail/status" + query, headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()["tabs"]


# ---------- 並び順 ----------

def test_default_order_is_newest_first_with_id_tiebreak(client, users, tmp_path):
    _, admin, _ = users
    same = "2026-09-01T00:00:00+00:00"
    old_high = _add_mail(tmp_path, n=1, priority=1, received_at="2026-08-01T00:00:00+00:00")
    low_new = _add_mail(tmp_path, n=2, priority=3, received_at="2026-09-02T00:00:00+00:00")
    tie_a = _add_mail(tmp_path, n=3, received_at=same)
    tie_b = _add_mail(tmp_path, n=4, received_at=same)
    # 時差の書き方が違っても、同じ時刻の並びとして比べる (+09:00 の 09:30 = UTC 00:30)
    jst = _add_mail(tmp_path, n=5, received_at="2026-09-01T09:30:00+09:00",
                    fetched_at="2026-09-01T00:31:00+00:00")
    expect = [low_new, jst, tie_b, tie_a, old_high]
    assert _ids(client, admin) == expect
    assert _ids(client, admin, "?sort=date") == expect
    # 重要度順: 高→中→低、同じ優先度は新しい順 (同じ時刻なら後から取り込んだ方が上)
    assert _ids(client, admin, "?sort=priority") == [old_high, jst, tie_b, tie_a, low_new]
    # 並び順を変えても、優先度は一覧に出る
    rows = client.get("/api/mail", headers=auth(admin)).json()
    assert {r["id"]: r["priority"] for r in rows}[old_high] == 1


@pytest.mark.parametrize("bad", ["bogus", "received_at", "priority DESC",
                                 "date; DROP TABLE mails", "DATE"])
def test_unknown_sort_is_rejected(client, users, tmp_path, bad):
    _, admin, _ = users
    _add_mail(tmp_path, n=1)
    r = client.get("/api/mail", params={"sort": bad}, headers=auth(admin))
    assert r.status_code == 400 and "sort" in r.json()["detail"]
    assert len(_ids(client, admin)) == 1          # 表は無事


def test_future_dated_mail_does_not_pin_itself_to_top(client, users, tmp_path):
    _, admin, _ = users
    now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    # 迷惑メール: 取り込んだのは3時間前なのに、日付は1年先
    spam = _add_mail(tmp_path, n=1, received_at=iso(now + timedelta(days=365)),
                     fetched_at=iso(now - timedelta(hours=3)))
    newer = _add_mail(tmp_path, n=2, received_at=iso(now - timedelta(hours=1)),
                      fetched_at=iso(now - timedelta(hours=1)))
    older = _add_mail(tmp_path, n=3, received_at=iso(now - timedelta(hours=5)),
                      fetched_at=iso(now - timedelta(hours=5)))
    # 時計が少し進んでいる送り主 (1日以内の先の日付) は、付けた日付のまま並べる
    skew = _add_mail(tmp_path, n=4, received_at=iso(now + timedelta(hours=2)),
                     fetched_at=iso(now - timedelta(hours=4)))
    # 日付が読めない行は、取り込んだ時刻で並べる (一覧から消えない)
    broken = _add_mail(tmp_path, n=5, received_at="garbage",
                       fetched_at=iso(now - timedelta(hours=2)))
    assert _ids(client, admin) == [skew, newer, broken, spam, older]
    # 重要度順でも同じ考え方 (同じ優先度の中で)
    assert _ids(client, admin, "?sort=priority") == [skew, newer, broken, spam, older]
    # 一覧に出す日付は、届いたメールに付いていたまま
    rows = {r["id"]: r for r in client.get("/api/mail", headers=auth(admin)).json()}
    assert rows[spam]["received_at"].startswith("2027-10-26")


def test_limit_keeps_newest_when_sorting_by_date(client, users, tmp_path):
    from server import db
    _, admin, _ = users
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        for i in range(520):
            # 古いメールほど優先度[高] (優先度順の500通とは違う顔ぶれになる)
            t = (base + timedelta(hours=i)).isoformat()
            conn.execute(
                "INSERT INTO mails (message_id, from_addr, from_name, subject, body,"
                " received_at, fetched_at, priority) VALUES (?,?,?,?,?,?,?,?)",
                (f"<bulk{i}@example.com>", f"s{i}@example.com", "", f"件名{i}", "",
                 t, t, 1 if i < 30 else 2))
    rows = client.get("/api/mail", headers=auth(admin)).json()
    assert len(rows) == 500
    assert rows[0]["subject"] == "件名519" and rows[-1]["subject"] == "件名20"
    rows = client.get("/api/mail?sort=priority", headers=auth(admin)).json()
    assert len(rows) == 500 and rows[0]["subject"] == "件名29"


def test_sort_works_with_tab_filters(client, users, tmp_path):
    _, admin, _ = users
    a = _add_mail(tmp_path, n=1, priority=1)
    b = _add_mail(tmp_path, n=2, priority=3)
    c = _add_mail(tmp_path, n=3, priority=1)
    _set(tmp_path, a, category="清掃のご依頼")
    _set(tmp_path, b, category="清掃のご依頼")
    _set(tmp_path, c, category="営業・宣伝")
    q = "?category=" + "清掃のご依頼"
    assert _ids(client, admin, q) == [b, a]
    assert _ids(client, admin, q + "&sort=priority") == [a, b]
    # 「重要（高）」のタブ (priority=1) は種類をまたいで出す
    assert _ids(client, admin, "?priority=1&status=unhandled") == [c, a]


# ---------- タブの数 (GET /api/mail/status の tabs) ----------

def test_tab_counts_by_category_high_and_status(client, users, tmp_path):
    _, admin, _ = users
    assert _tabs(client, admin) == {"all": 0, "high": 0, "categories": {},
                                    "uncategorized": 0, "has_uncategorized": False}
    m1 = _add_mail(tmp_path, n=1, priority=1)                           # 清掃・高・未対応
    m2 = _add_mail(tmp_path, n=2, status="pending_approval")            # 清掃・確認待ち
    m3 = _add_mail(tmp_path, n=3, status="replied", priority=1)         # 清掃・返信済み (数えない)
    m4 = _add_mail(tmp_path, n=4, status="archived")                    # 予約・対応不要 (数えない)
    m5 = _add_mail(tmp_path, n=5, priority=1, status="pending_approval")  # 予約・高・確認待ち
    for mid in (m1, m2, m3):
        _set(tmp_path, mid, category="清掃のご依頼")
    for mid in (m4, m5):
        _set(tmp_path, mid, category="予約・宿泊のお問い合わせ")
    t = _tabs(client, admin)
    assert t == {"all": 3, "high": 2,
                 "categories": {"清掃のご依頼": 2, "予約・宿泊のお問い合わせ": 1},
                 "uncategorized": 0, "has_uncategorized": False}
    # 未分類 (この機能より前に届いたメール)。返信済みだけでも「未分類」のタブは出す
    m6 = _add_mail(tmp_path, n=6, status="replied")
    t = _tabs(client, admin)
    assert t["has_uncategorized"] is True and t["uncategorized"] == 0
    _add_mail(tmp_path, n=7, priority=1)
    t = _tabs(client, admin)
    assert t["uncategorized"] == 1 and t["all"] == 4 and t["high"] == 3
    assert "" not in t["categories"]
    # 未分類を全部、種類に入れたら「未分類」のタブは消える
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET category = 'その他' WHERE category = ''")
    t = _tabs(client, admin)
    assert t["has_uncategorized"] is False and t["categories"]["その他"] == 1
    assert m6


def test_tab_counts_follow_assignee_and_account_filters(client, users, tmp_path):
    from server import db
    _, admin, staff = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        acct = conn.execute(
            "INSERT INTO mail_accounts (label, imap_host, imap_user, imap_pass, created_at)"
            " VALUES ('予約用', 'imap.example.com', 'yoyaku@example.com', 'pw', '2026-10-01T00:00:00+00:00')").lastrowid
    a = _add_mail(tmp_path, n=1, priority=1)
    b = _add_mail(tmp_path, n=2)
    c = _add_mail(tmp_path, n=3)
    _set(tmp_path, a, category="清掃のご依頼", assignee_user_id=staff["id"], account_id=acct)
    _set(tmp_path, b, category="清掃のご依頼")
    _set(tmp_path, c, category="営業・宣伝", account_id=acct)
    t = _tabs(client, admin, f"?account={acct}")
    assert t["all"] == 2 and t["categories"] == {"清掃のご依頼": 1, "営業・宣伝": 1}
    assert t["high"] == 1
    t = _tabs(client, admin, "?account=none")
    assert t["all"] == 1 and t["categories"] == {"清掃のご依頼": 1}
    t = _tabs(client, staff, "?assignee=me")
    assert t == {"all": 1, "high": 1, "categories": {"清掃のご依頼": 1},
                 "uncategorized": 0, "has_uncategorized": False}
    t = _tabs(client, admin, f"?assignee=none&account={acct}")
    assert t["all"] == 1 and t["categories"] == {"営業・宣伝": 1}
    # 受信箱の上の数 (未対応・確認待ち) は絞り込みに関係なく全部
    s = client.get(f"/api/mail/status?account={acct}", headers=auth(admin)).json()
    assert s["unhandled"] == 3
    # おかしな値は 400 (一覧と同じ)
    for q in ("?account=abc", "?assignee=x1", "?account=1 OR 1=1"):
        assert client.get("/api/mail/status" + q, headers=auth(admin)).status_code == 400


def test_tab_counts_permissions(client, users, tmp_path):
    worker, admin, staff = users
    _add_mail(tmp_path, n=1)
    # 管理者とメール担当者だけが数を見られる
    assert _tabs(client, admin)["all"] == 1
    assert _tabs(client, staff)["all"] == 1
    assert client.get("/api/mail/status", headers=auth(worker)).status_code == 403
    assert client.get("/api/mail/status").status_code == 401
    assert client.get("/api/mail/status?account=1").status_code == 401


def test_tab_counts_do_not_write(client, users, tmp_path):
    """1分ごとに呼ばれるので、タブの数を数えても DB に書き込まない (書き込みのロック中でも返る)."""
    import sqlite3
    _, admin, _ = users
    _add_mail(tmp_path, n=1)
    _tabs(client, admin)    # 端末などの初回の書き込みを先に済ませる
    hold = sqlite3.connect(tmp_path / "zatsumu.db", timeout=0)
    hold.execute("BEGIN IMMEDIATE")
    try:
        assert _tabs(client, admin, "?assignee=none")["all"] == 1
    finally:
        hold.rollback()
        hold.close()


# ---------- 画面 ----------

def test_mail_page_has_tabs_and_sort_toggle(client):
    page = client.get("/mail").text
    # 「種類」の選択欄はやめて、タブ1つにする
    assert 'id="filter-cat"' not in page
    assert 'id="cat-tabs"' in page and 'role="tablist"' in page
    assert '"重要（高）"' in page and '"未分類"' in page and '"すべて"' in page
    # 状態・担当・アドレスの絞り込みは残す
    for marker in ('id="filter"', 'id="filter-asg"', 'id="filter-acct"'):
        assert marker in page
    # 並び順 (既定は新しい順) と、タブ・並び順の記憶 (使えないブラウザでも動く)
    assert 'data-sort="date" aria-pressed="true"' in page
    assert 'data-sort="priority"' in page and "重要度順" in page
    assert 'q.set("sort", sortMode);' in page
    assert "try { return localStorage.getItem(key); } catch { return null; }" in page
    assert "try { localStorage.setItem(key, v); } catch {}" in page
    # タブの数は1分ごとの軽い問い合わせ (受信の様子) から。種類の名前は textContent で入れる
    assert 'api("/api/mail/status" + scopeQuery())' in page
    assert "name.textContent = label;" in page
    # 狭い画面ではタブの列だけを横にずらす
    assert "flex-wrap: nowrap; overflow-x: auto;" in page
    # 確認待ちを見るときはタブを「すべて」に戻す
    assert 'setTab(TAB_ALL);\n  renderTabs();\n  showInbox();' in page
