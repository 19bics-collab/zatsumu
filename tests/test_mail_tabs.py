"""受信箱のタブ (種類ごと・重要（高）) と並び順 (新しい順・重要度順) のテスト.

- GET /api/mail の sort (date=新しい順が既定 / priority=重要度順。ほかの値は 400)
- 未来の日付を付けたメール (迷惑メールなど) が、新しい順の一番上に居座らないこと
  (少しだけ先の日付でも、後から届いた本物の新着より上に出ないこと)
- 新しい順で500通を超えるときは、新しい方の500通を出すこと
- GET /api/mail/status の tabs (種類ごと・重要・未分類の、未対応と確認待ちの数。
  「状態」で未対応か確認待ちを選んでいるときはその状態だけ) と権限
- 未分類のメールが無くても、タブの数を数えるのに表を全部読まないこと (種類の索引)
- hide_low=1 (受信箱の「重要度」の「高・中」) で優先度[低]を一覧・タブの数から外すこと。
  確認待ちの[低]と、差し戻した[低] (メール担当者が直して頼み直す) は外さないこと
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
    # 少しだけ先の日付 (取り込んだ時刻の20時間後) も、取り込んだ時刻で並べる
    near = _add_mail(tmp_path, n=4, received_at=iso(now + timedelta(hours=16)),
                     fetched_at=iso(now - timedelta(hours=4)))
    # 日付が読めない行は、取り込んだ時刻で並べる (一覧から消えない)
    broken = _add_mail(tmp_path, n=5, received_at="garbage",
                       fetched_at=iso(now - timedelta(hours=2)))
    assert _ids(client, admin) == [newer, broken, spam, near, older]
    # 重要度順でも同じ考え方 (同じ優先度の中で)
    assert _ids(client, admin, "?sort=priority") == [newer, broken, spam, near, older]
    # 一覧に出す日付は、届いたメールに付いていたまま
    rows = {r["id"]: r for r in client.get("/api/mail", headers=auth(admin)).json()}
    assert rows[spam]["received_at"].startswith("2027-10-26")


def test_slightly_future_date_stays_below_real_new_mail(client, users, tmp_path):
    """日付を「取り込みの20時間後」にした迷惑メールが、後から届いた本物の新着より上に出ない."""
    _, admin, _ = users
    now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    spam = _add_mail(tmp_path, n=1, received_at=iso(now + timedelta(hours=17)),
                     fetched_at=iso(now - timedelta(hours=3)))
    new1 = _add_mail(tmp_path, n=2, received_at=iso(now - timedelta(minutes=6)),
                     fetched_at=iso(now - timedelta(minutes=5)))
    new2 = _add_mail(tmp_path, n=3, received_at=iso(now - timedelta(minutes=2)),
                     fetched_at=iso(now - timedelta(minutes=1)))
    assert _ids(client, admin) == [new2, new1, spam]
    # 送り主の時計が数分進んでいても、取り込んだ時刻の順で並ぶ (本物どうしの順は変わらない)
    fast = _add_mail(tmp_path, n=4, received_at=iso(now + timedelta(minutes=3)),
                     fetched_at=iso(now - timedelta(seconds=30)))
    assert _ids(client, admin) == [fast, new2, new1, spam]


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
    assert _tabs(client, admin) == {"all": 0, "high": 0, "flagged": 0, "categories": {},
                                    "uncategorized": 0, "has_uncategorized": False,
                                    "statuses": ["unhandled", "pending_approval"]}
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
    assert t == {"all": 3, "high": 2, "flagged": 0,
                 "categories": {"清掃のご依頼": 2, "予約・宿泊のお問い合わせ": 1},
                 "uncategorized": 0, "has_uncategorized": False,
                 "statuses": ["unhandled", "pending_approval"]}
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
    assert t == {"all": 1, "high": 1, "flagged": 0, "categories": {"清掃のご依頼": 1},
                 "uncategorized": 0, "has_uncategorized": False,
                 "statuses": ["unhandled", "pending_approval"]}
    t = _tabs(client, admin, f"?assignee=none&account={acct}")
    assert t["all"] == 1 and t["categories"] == {"営業・宣伝": 1}
    # 受信箱の上の数 (未対応・確認待ち) は絞り込みに関係なく全部
    s = client.get(f"/api/mail/status?account={acct}", headers=auth(admin)).json()
    assert s["unhandled"] == 3
    # おかしな値は 400 (一覧と同じ)
    for q in ("?account=abc", "?assignee=x1", "?account=1 OR 1=1"):
        assert client.get("/api/mail/status" + q, headers=auth(admin)).status_code == 400


def test_tab_counts_follow_status_filter(client, users, tmp_path):
    """「状態」が未対応なら、タブの数も未対応だけ (押したときの一覧の通数とそろえる)."""
    _, admin, staff = users
    a = _add_mail(tmp_path, n=1, priority=1)                           # 清掃・高・未対応
    b = _add_mail(tmp_path, n=2, status="pending_approval")            # 清掃・確認待ち
    c = _add_mail(tmp_path, n=3, status="pending_approval", priority=1)  # 予約・高・確認待ち
    d = _add_mail(tmp_path, n=4, status="replied")                     # 予約・返信済み
    for mid in (a, b):
        _set(tmp_path, mid, category="清掃のご依頼")
    for mid in (c, d):
        _set(tmp_path, mid, category="予約・宿泊のお問い合わせ")
    for user in (admin, staff):
        t = _tabs(client, user, "?status=unhandled")
        assert t["statuses"] == ["unhandled"]
        assert t["all"] == 1 and t["high"] == 1
        assert t["categories"] == {"清掃のご依頼": 1}
        # タブの数と、そのタブを押したときの一覧の通数が同じ
        q = "?status=unhandled&category=" + "清掃のご依頼"
        assert len(_ids(client, user, q)) == t["categories"]["清掃のご依頼"]
        assert _ids(client, user, "?status=unhandled&category=予約・宿泊のお問い合わせ") == []
        assert len(_ids(client, user, "?status=unhandled")) == t["all"]
        t = _tabs(client, user, "?status=pending_approval")
        assert t["statuses"] == ["pending_approval"]
        assert t["all"] == 2 and t["high"] == 1
        assert t["categories"] == {"清掃のご依頼": 1, "予約・宿泊のお問い合わせ": 1}
        assert len(_ids(client, user, "?status=pending_approval")) == t["all"]
    # 返信済み・対応不要・すべてのときは、未対応と確認待ちを合わせて数える
    for q in ("?status=replied", "?status=archived", "?status=", ""):
        t = _tabs(client, admin, q)
        assert t["all"] == 3 and t["statuses"] == ["unhandled", "pending_approval"]
    # 担当の絞り込みと合わせても効く
    _set(tmp_path, b, assignee_user_id=staff["id"])
    t = _tabs(client, staff, "?status=pending_approval&assignee=me")
    assert t["all"] == 1 and t["categories"] == {"清掃のご依頼": 1}
    # おかしな状態は 400 (一覧と同じ)
    for bad in ("bogus", "unhandled' OR 1=1"):
        r = client.get("/api/mail/status", params={"status": bad}, headers=auth(admin))
        assert r.status_code == 400


def test_tab_counts_use_category_index(client, users, tmp_path):
    """未分類のメールが1通もなくても、「未分類があるか」を表を全部読まずに確かめる.

    新しく届くメールには必ず種類が付くので、未分類0通がふつうになる。索引がないと
    1分ごとの /api/mail/status が、メールが増えるほど遅くなる (受信の書き込みも待たされる)。
    """
    from server import db
    _, admin, _ = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mails'")}
        assert "idx_mails_category" in names
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT EXISTS (SELECT 1 FROM mails m WHERE m.category = '')"))
        assert "idx_mails_category" in plan and "SCAN m" not in plan
        # 何度起動しても (移行をもう一度しても) 困らない
        db._migrate(conn)
    _add_mail(tmp_path, n=1)
    _set(tmp_path, 1, category="その他")
    assert _tabs(client, admin)["has_uncategorized"] is False


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
    # タブの数は「状態」(未対応・確認待ち) にもそろえる。何も出ないときは「状態」も案内する
    assert '[["status", "filter"], ["assignee", "filter-asg"], ["account", "filter-acct"]]' in page
    assert '[[$("filter").value, "「状態」"]' in page
    assert "上の${narrowed}を「すべて」に戻すと全部見られます" in page
    assert "name.textContent = label;" in page
    # 狭い画面ではタブの列だけを横にずらす
    assert "flex-wrap: nowrap; overflow-x: auto;" in page
    # 確認待ちを見るときはタブを「すべて」に戻す
    assert 'setTab(TAB_ALL);\n  renderTabs();\n  showInbox();' in page


# ---------- 重要度[低]を出さない (受信箱の「重要度」: 高・中 / すべて) ----------

def test_hide_low_list_and_hidden_count(client, users, tmp_path):
    _, admin, staff = users
    high = _add_mail(tmp_path, n=1, priority=1)
    mid = _add_mail(tmp_path, n=2, priority=2)
    low = _add_mail(tmp_path, n=3, priority=3)
    low_done = _add_mail(tmp_path, n=4, priority=3, status="replied")
    # 確認待ち (メール担当者からの確認のお願い) は[低]でも出す (隠すと管理者が承認できない)
    low_pend = _add_mail(tmp_path, n=5, priority=3, status="pending_approval")
    _set(tmp_path, low, category="営業・宣伝")
    _set(tmp_path, low_pend, category="営業・宣伝")
    # 送らなければ今までどおり全部 (ヘッダも付けない)
    r = client.get("/api/mail", headers=auth(admin))
    assert {m["id"] for m in r.json()} == {high, mid, low, low_done, low_pend}
    assert "X-Hidden-Low" not in r.headers
    # hide_low=1 で[低]を出さず、出さなかった数をヘッダで返す (ほかの絞り込みに合うものだけ数える)
    for user in (admin, staff):
        r = client.get("/api/mail?hide_low=1", headers=auth(user))
        assert r.status_code == 200, r.text
        assert [m["id"] for m in r.json()] == [low_pend, mid, high]
        assert r.headers["X-Hidden-Low"] == "2"
    r = client.get("/api/mail?hide_low=1&status=unhandled", headers=auth(admin))
    assert r.headers["X-Hidden-Low"] == "1" and len(r.json()) == 2
    # 「確認待ち」を押したとき (状態=確認待ち・[低]を出さない) も、[低]の確認待ちが出る
    r = client.get("/api/mail?hide_low=1&status=pending_approval", headers=auth(admin))
    assert [m["id"] for m in r.json()] == [low_pend] and r.headers["X-Hidden-Low"] == "0"
    r = client.get("/api/mail?hide_low=1&category=" + "営業・宣伝", headers=auth(admin))
    assert [m["id"] for m in r.json()] == [low_pend] and r.headers["X-Hidden-Low"] == "1"
    r = client.get("/api/mail?hide_low=1&priority=1", headers=auth(admin))
    assert [m["id"] for m in r.json()] == [high] and r.headers["X-Hidden-Low"] == "0"
    # 重要度順でも、検索と一緒でも効く
    assert _ids(client, admin, "?hide_low=1&sort=priority") == [high, mid, low_pend]
    r = client.get("/api/mail?hide_low=1&q=" + "件名", headers=auth(admin))
    assert {m["id"] for m in r.json()} == {high, mid, low_pend} and r.headers["X-Hidden-Low"] == "2"
    assert _ids(client, admin, "?hide_low=0") == [low_pend, low_done, low, mid, high]


def test_hide_low_tab_counts(client, users, tmp_path):
    _, admin, _ = users
    _add_mail(tmp_path, n=1, priority=1)
    m2 = _add_mail(tmp_path, n=2, priority=3)
    m3 = _add_mail(tmp_path, n=3, priority=3, status="pending_approval")
    m4 = _add_mail(tmp_path, n=4, priority=2)
    for mid in (m2, m3):
        _set(tmp_path, mid, category="営業・宣伝")
    _set(tmp_path, m4, category="清掃のご依頼")
    t = _tabs(client, admin)
    assert t["all"] == 4 and t["categories"] == {"営業・宣伝": 2, "清掃のご依頼": 1}
    # [低]を数えない。ただし確認待ちの[低] (m3) は一覧に出すので数える
    t = _tabs(client, admin, "?hide_low=1")
    assert t["all"] == 3 and t["high"] == 1 and t["uncategorized"] == 1
    assert t["categories"] == {"営業・宣伝": 1, "清掃のご依頼": 1}
    # 状態の絞り込みとも一緒に効く (「確認待ち」を押したときのタブの数と一覧がそろう)。
    # 未分類のタブを出すか (has_uncategorized) は変えない
    t2 = _tabs(client, admin, "?hide_low=1&status=pending_approval")
    assert t2["all"] == 1 and t2["categories"] == {"営業・宣伝": 1}
    assert _ids(client, admin, "?hide_low=1&status=pending_approval") == [m3]
    assert _tabs(client, admin, "?hide_low=1&status=unhandled")["all"] == 2
    assert t["has_uncategorized"] is True


def test_hide_low_keeps_sent_back_mail(client, users, tmp_path):
    """差し戻した[低]のメールは、「高・中」の表示でもメール担当者に出す (差し戻しとコメントに気付くように)."""
    _, admin, staff = users
    mid = _add_mail(tmp_path, n=1, priority=2)
    low = _add_mail(tmp_path, n=2, priority=3)
    back = _add_mail(tmp_path, n=3, priority=3, status="pending_approval")
    # メール担当者が確認をお願いし、管理者が差し戻す (未対応に戻り、コメントが付く)
    _set(tmp_path, back, requested_by="suzuki", draft_reply="下書き")
    r = client.post(f"/api/mail/{back}/send-back", json={"note": "宛名を直してください"}, headers=auth(admin))
    assert r.status_code == 200, r.text
    # 返信済み・対応不要にしたものは、差し戻しのコメントが残っていても出さない
    done = _add_mail(tmp_path, n=4, priority=3, status="archived")
    _set(tmp_path, done, return_note="前のコメント")
    for user in (admin, staff):
        r = client.get("/api/mail?status=unhandled&hide_low=1", headers=auth(user))
        assert r.status_code == 200, r.text
        rows = {m["id"]: m for m in r.json()}
        assert set(rows) == {back, mid} and rows[back]["returned"] is True
        assert r.headers["X-Hidden-Low"] == "1"   # 差し戻していない[低] (low) だけ
        r = client.get("/api/mail?hide_low=1", headers=auth(user))
        assert {m["id"] for m in r.json()} == {back, mid} and r.headers["X-Hidden-Low"] == "2"
        # タブの数も一覧とそろえる
        assert _tabs(client, user, "?status=unhandled&hide_low=1")["all"] == 2
        assert _tabs(client, user, "?hide_low=1")["all"] == 2
    # 頼み直すと確認待ちに戻り、コメントは消える (確認待ちなので[低]でも出す)
    _set(tmp_path, back, status="pending_approval", return_note="")
    assert _ids(client, admin, "?hide_low=1") == [back, mid]


def test_mail_page_hides_low_by_default_and_colors_priority(client):
    page = client.get("/mail").text
    # 「重要度」の切り替え: 既定は「高・中」。このブラウザに覚えておく
    assert 'id="low-hide" data-low="hide" aria-pressed="true">高・中</button>' in page
    assert 'id="low-show" data-low="show" aria-pressed="false">すべて</button>' in page
    assert 'let showLow = loadPref(LOW_KEY) === "show";' in page
    assert 'savePref(LOW_KEY, show ? "show" : "hide");' in page
    # 一覧とタブの数の両方に hide_low を付ける (探しているときは[低]からも探し、タブの数にも[低]を
    # 入れて、探した一覧とそろえる。探し始めたか (searchStatus) で決め、打っている途中の言葉では変えない)
    assert 'if (word) q.set("q", word);\n  else if (!showLow) q.set("hide_low", "1");' in page
    i = page.index("function scopeQuery() {")
    scope = page[i:page.index("\n}", i)]
    assert 'if (!showLow && !searchStatus) q.set("hide_low", "1");\n  return q.toString() ? "?" + q : "";' in scope
    assert scope.count("hide_low") == 1
    # 探し始める・やめるときは、searchStatus を変えてから受信箱を出し直す (受信の様子を聞き直してから
    # 一覧を取るので、タブの数と一覧が同じ絞り込みになる)
    assert "syncSearchStatus(word);\n  showInbox().catch(() => {});" in page
    assert "searchStatus = null;" in page[page.index("function showPending() {"):page.index("$(\"approval-show\")")]
    assert "確認待ち・差し戻したメール・★のメールは低でも出します" in page
    # 指で押す画面では、並び順・重要度のボタンを押しやすい高さにする
    assert "@media (pointer: coarse) { .sort-tg button { min-height: 40px; padding: .45rem .7rem; } }" in page
    assert page.count('hiddenLow = Number(r.headers.get("X-Hidden-Low")) || 0;') == 2
    assert 'id="low-note" class="low-note" role="status" hidden' in page
    assert '[inboxHiddenLow > 0, "「重要度」"]' in page
    # 並び順のボタンと「重要度」のボタンは別々に動かす
    assert 'document.querySelectorAll(".sort-tg button[data-sort]")' in page
    assert 'document.querySelectorAll(".sort-tg button[data-low]")' in page
    assert 'document.querySelectorAll(".sort-tg button")' not in page
    # 色: 優先度の欄はバッジ、行の左に帯 (高=赤・中=だいだい・低=灰色)。[高]の未対応の行は地も赤
    assert '<tr class="mail-row p${Number(m.priority)}' in page
    assert '<span class="prb pr-${Number(m.priority)}">${esc(PR[m.priority] || m.priority)}</span>' in page
    for rule in (".prb.pr-1 { background: #dc2626; color: #fff; }",
                 ".mail-row.p1 > td:first-child { box-shadow: inset 4px 0 0 #dc2626; }",
                 ".mail-row.p2 > td:first-child { box-shadow: inset 4px 0 0 #f59e0b; }",
                 ".mail-row.p3 > td:first-child { box-shadow: inset 4px 0 0 #d1d5db; }",
                 ".table-wrap tr.mail-row.p1 { border-left: 5px solid #dc2626; }",
                 "#m-priority[data-p=\"1\"]"):
        assert rule in page, rule
    assert '$("m-priority").dataset.p = m.priority;' in page
    assert '$("m-priority").dataset.p = $("m-priority").value;' in page
