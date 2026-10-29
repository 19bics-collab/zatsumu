"""よく使うアドレス (アドレス帳) と、宛先・CC・BCC の候補のテスト.

- アドレス帳の登録・変更・削除 (管理者だけ。メール担当者は見る・候補に使うだけ)
- アドレスの確かめ方 (CC と同じ。「名前 <アドレス>」も可・1つだけ・同じアドレスは1つ)
- 候補: アドレス帳が先、次に前にやり取りした人 (やり取りの多さ・新しさの順)。
  自分たちのアドレスとフォーム・自動送信の送信元は出さない
- 索引だけで探す (メールが何万通でも本文の表を読まない)
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
    """一般メンバー・メール担当者・管理者."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        staff = db.create_user(conn, "田中")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        admin = db.create_user(conn, "黒須", is_admin=True)
    return worker, staff, admin


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


_seq = [100]


def _mail(tmp_path, addr, name="", days_ago=1.0, **kw):
    _seq[0] += 1
    at = _iso(days_ago)
    return _add_mail(tmp_path, n=_seq[0], from_addr=addr, from_name=name,
                     message_id=f"<ab{_seq[0]}@example.com>", received_at=at, fetched_at=at, **kw)


def _replied(tmp_path, mid, to="", cc="", bcc="", days_ago=1.0):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET status = 'replied', replied_at = ?, reply_sent_to = ?,"
                     " reply_cc = ?, reply_bcc = ? WHERE id = ?",
                     (_iso(days_ago), to, cc, bcc, mid))


def _add(client, admin, email, name="", note=""):
    return client.post("/api/mail/address-book", headers=auth(admin),
                       json={"email": email, "name": name, "note": note})


def _suggest(client, user, q, **params):
    r = client.get("/api/mail/address-suggest", headers=auth(user), params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _emails(items):
    return [i["email"] for i in items]


# ---------- 登録・変更・削除 ----------

def test_admin_adds_edits_and_deletes(client, users):
    _, staff, admin = users
    r = _add(client, admin, " Taro@ABC.example.jp ", "山田 太郎", "清掃の担当")
    assert r.status_code == 200, r.text
    e = r.json()
    assert (e["email"], e["name"], e["note"]) == ("Taro@ABC.example.jp", "山田 太郎", "清掃の担当")
    # 一覧 (メール担当者も見られる。名前の順で、名前の無いものは後ろ)
    assert _add(client, admin, "zz@example.jp").status_code == 200
    assert _add(client, admin, "aa@example.jp", "あべ").status_code == 200
    items = client.get("/api/mail/address-book", headers=auth(staff)).json()["items"]
    assert [i["email"] for i in items] == ["aa@example.jp", "Taro@ABC.example.jp", "zz@example.jp"]
    # 変える (渡したものだけ)
    r = client.patch(f"/api/mail/address-book/{e['id']}", headers=auth(admin),
                     json={"note": "  平日のみ  "})
    assert r.status_code == 200 and r.json()["note"] == "平日のみ"
    assert r.json()["name"] == "山田 太郎"
    r = client.patch(f"/api/mail/address-book/{e['id']}", headers=auth(admin),
                     json={"email": "taro2@abc.example.jp", "name": "山田"})
    assert (r.json()["email"], r.json()["name"]) == ("taro2@abc.example.jp", "山田")
    assert client.patch(f"/api/mail/address-book/{e['id']}", headers=auth(admin),
                        json={}).status_code == 400
    # 消す
    assert client.delete(f"/api/mail/address-book/{e['id']}",
                         headers=auth(admin)).status_code == 200
    assert client.delete(f"/api/mail/address-book/{e['id']}",
                         headers=auth(admin)).status_code == 404
    assert client.patch("/api/mail/address-book/9999", headers=auth(admin),
                        json={"note": "x"}).status_code == 404
    items = client.get("/api/mail/address-book", headers=auth(admin)).json()["items"]
    assert len(items) == 2


def test_operations_are_audited(client, users, tmp_path):
    from server import db
    _, _, admin = users
    eid = _add(client, admin, "taro@abc.example.jp", "山田").json()["id"]
    client.patch(f"/api/mail/address-book/{eid}", headers=auth(admin), json={"note": "x"})
    client.delete(f"/api/mail/address-book/{eid}", headers=auth(admin))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        acts = [r[0] for r in conn.execute(
            "SELECT action FROM audit_log WHERE action LIKE 'address_book_%' ORDER BY id")]
    assert acts == ["address_book_create", "address_book_update", "address_book_delete"]


def test_only_admins_change_staff_reads_members_nothing(client, users):
    worker, staff, admin = users
    eid = _add(client, admin, "taro@abc.example.jp", "山田").json()["id"]
    # メール担当者: 見る・候補は使える。変えるのはだめ
    assert client.get("/api/mail/address-book", headers=auth(staff)).status_code == 200
    assert client.get("/api/mail/address-suggest?q=ya", headers=auth(staff)).status_code == 200
    assert _add(client, staff, "x@example.jp").status_code == 403
    assert client.patch(f"/api/mail/address-book/{eid}", headers=auth(staff),
                        json={"note": "x"}).status_code == 403
    assert client.delete(f"/api/mail/address-book/{eid}", headers=auth(staff)).status_code == 403
    # 一般メンバー・トークンなし: 何もできない
    for h in (auth(worker), {}):
        want = 403 if h else 401
        assert client.get("/api/mail/address-book", headers=h).status_code == want
        assert client.get("/api/mail/address-suggest?q=ya", headers=h).status_code == want
        assert client.post("/api/mail/address-book", headers=h,
                           json={"email": "x@example.jp"}).status_code == want
    assert len(client.get("/api/mail/address-book", headers=auth(admin)).json()["items"]) == 1


@pytest.mark.parametrize("email, msg", [
    ("", "メールアドレスを入れてください"),
    ("taro", "正しくありません"),
    ("taro@", "正しくありません"),
    ("a@b.jp, c@d.jp", "1つだけ"),
    ("a@b.jp; c@d.jp", "1つだけ"),
    ("a@b.jp\nBcc: x@evil.jp", "改行"),
    ("山田 <taro@abc.example.jp", "正しくありません"),
    ("x" * 70 + "@example.jp", "正しくありません"),
])
def test_bad_addresses_are_refused(client, users, email, msg):
    _, _, admin = users
    r = _add(client, admin, email)
    assert r.status_code == 400 and msg in r.json()["detail"], r.text


def test_name_address_form_and_fullwidth_are_accepted(client, users):
    _, _, admin = users
    # メールソフトからコピーした形。名前の欄が空なら、その名前を使う
    r = _add(client, admin, '"Yamada, Taro" <taro@abc.example.jp>')
    assert r.status_code == 200, r.text
    assert (r.json()["email"], r.json()["name"]) == ("taro@abc.example.jp", "Yamada, Taro")
    # 日本語入力の全角は半角にする。名前の欄があれば、そちら
    r = _add(client, admin, "ｈａｎａ＠ｅｘａｍｐｌｅ．ｊｐ", "花子")
    assert (r.json()["email"], r.json()["name"]) == ("hana@example.jp", "花子")


def test_same_address_only_once_ignoring_case(client, users):
    _, _, admin = users
    assert _add(client, admin, "taro@abc.example.jp", "山田").status_code == 200
    r = _add(client, admin, "TARO@abc.example.jp")
    assert r.status_code == 409 and "もうアドレス帳にあります（山田）" in r.json()["detail"]
    other = _add(client, admin, "hana@example.jp").json()["id"]
    r = client.patch(f"/api/mail/address-book/{other}", headers=auth(admin),
                     json={"email": "Taro@ABC.example.jp"})
    assert r.status_code == 409
    # 自分自身のアドレスの大文字・小文字を変えるのはよい
    r = client.patch(f"/api/mail/address-book/{other}", headers=auth(admin),
                     json={"email": "HANA@example.jp"})
    assert r.status_code == 200 and r.json()["email"] == "HANA@example.jp"


def test_name_and_note_limits(client, users):
    from server import address_book
    _, _, admin = users
    assert _add(client, admin, "a@example.jp", '山田 <太郎>').status_code == 400
    assert _add(client, admin, "a@example.jp", 'say "hi"').status_code == 400
    r = _add(client, admin, "a@example.jp", "あ" * (address_book.NAME_MAX_CHARS + 1))
    assert r.status_code == 400 and "文字以内" in r.json()["detail"]
    r = _add(client, admin, "a@example.jp", note="あ" * (address_book.NOTE_MAX_CHARS + 1))
    assert r.status_code == 400 and "メモ" in r.json()["detail"]
    # 改行・連続した空白は1つの空白に
    r = _add(client, admin, "a@example.jp", "山田\n  太郎", "清掃\n担当")
    assert (r.json()["name"], r.json()["note"]) == ("山田 太郎", "清掃 担当")


def test_count_limit(client, users, monkeypatch):
    from server import address_book
    _, _, admin = users
    monkeypatch.setattr(address_book, "BOOK_MAX_COUNT", 2)
    assert _add(client, admin, "a@example.jp").status_code == 200
    assert _add(client, admin, "b@example.jp").status_code == 200
    r = _add(client, admin, "c@example.jp")
    assert r.status_code == 400 and "2件まで" in r.json()["detail"]


def test_list_shows_client_name(client, users):
    _, staff, admin = users
    r = client.post("/api/mail/clients", headers=auth(admin),
                    json={"name": "ABC商事", "patterns": ["@abc.example.jp"]})
    assert r.status_code == 200, r.text
    _add(client, admin, "taro@abc.example.jp", "山田")
    _add(client, admin, "hana@gmail.com", "花子")
    items = {i["email"]: i for i in
             client.get("/api/mail/address-book", headers=auth(staff)).json()["items"]}
    assert items["taro@abc.example.jp"]["client"]["name"] == "ABC商事"
    assert items["hana@gmail.com"]["client"] is None


# ---------- 候補 ----------

def test_book_matches_come_first_then_history(client, users, tmp_path):
    _, staff, admin = users
    # 前にやり取りした人 (たくさん)
    for i in range(3):
        _mail(tmp_path, "tanaka@zzz.example.jp", "田中 一郎", days_ago=i + 1)
    _add(client, admin, "taro@abc.example.jp", "山田 太郎", "清掃の担当")
    items = _suggest(client, staff, "ta")
    assert _emails(items) == ["taro@abc.example.jp", "tanaka@zzz.example.jp"]
    assert items[0]["source"] == "book" and items[0]["note"] == "清掃の担当"
    assert items[1]["source"] == "history" and items[1]["name"] == "田中 一郎"
    assert items[1]["count"] == 3


def test_book_matches_name_note_email_and_client_name(client, users):
    _, staff, admin = users
    client.post("/api/mail/clients", headers=auth(admin),
                json={"name": "ABC商事", "patterns": ["@abc.example.jp"]})
    _add(client, admin, "keiri@abc.example.jp", "経理の窓口")
    _add(client, admin, "hana@gmail.com", "花子", "清掃の担当・平日のみ")
    _add(client, admin, "jiro@example.jp", "次郎")
    assert _emails(_suggest(client, staff, "経理")) == ["keiri@abc.example.jp"]
    assert _emails(_suggest(client, staff, "清掃")) == ["hana@gmail.com"]
    assert _emails(_suggest(client, staff, "abc商事")) == ["keiri@abc.example.jp"]
    assert _emails(_suggest(client, staff, "ＪＩＲＯ")) == ["jiro@example.jp"]   # 全角・大文字
    assert _suggest(client, staff, "") == []
    assert _suggest(client, staff, "   ") == []
    assert _suggest(client, staff, "どこにもない") == []


def test_word_start_matches_come_before_middle_matches(client, users):
    _, staff, admin = users
    _add(client, admin, "akita@example.jp", "秋田")      # 途中に「ta」
    _add(client, admin, "taro@example.jp", "太郎")       # 先頭が「ta」
    _add(client, admin, "s.tanaka@example.jp", "田中")   # 「.」の後が「ta」
    # 先頭から合うもの同士は名前の順 (太郎・田中)。途中に入っているだけのものは後ろ
    assert _emails(_suggest(client, staff, "ta")) == [
        "taro@example.jp", "s.tanaka@example.jp", "akita@example.jp"]


def test_history_ranks_by_frequency_and_recency(client, users, tmp_path):
    _, staff, _ = users
    # よくやり取りする人 (最近も)
    for i in range(5):
        _mail(tmp_path, "sato.often@example.jp", "佐藤", days_ago=i + 1)
    # 昔はたくさんやり取りしたが、ここ1年は無い人
    for i in range(6):
        _mail(tmp_path, "sato.old@example.jp", "佐藤 旧", days_ago=400 + i)
    # 1回だけ、昨日
    _mail(tmp_path, "sato.once@example.jp", "佐藤 一回", days_ago=1)
    assert _emails(_suggest(client, staff, "sato")) == [
        "sato.often@example.jp", "sato.once@example.jp", "sato.old@example.jp"]
    # 名前でも探せる (名前は一番新しいメールのもの)
    assert "sato.often@example.jp" in _emails(_suggest(client, staff, "佐藤"))


def test_history_includes_addresses_we_sent_to(client, users, tmp_path):
    _, staff, _ = users
    mid = _mail(tmp_path, "guest@example.jp", "ゲスト")
    _replied(tmp_path, mid, to="guest.new@example.jp",
             cc="cleaner@clean.example.jp, owner@example.jp", bcc="boss.bcc@example.jp")
    assert _emails(_suggest(client, staff, "clean")) == ["cleaner@clean.example.jp"]
    assert _emails(_suggest(client, staff, "boss")) == ["boss.bcc@example.jp"]
    assert _emails(_suggest(client, staff, "guest.new")) == ["guest.new@example.jp"]
    # 前の版のデモなどで「名前 <アドレス>」の形で残っていても、アドレスにする
    mid3 = _mail(tmp_path, "guest3@example.jp")
    _replied(tmp_path, mid3, cc="佐々木 部長 <bucho@sasaki.example.jp>")
    assert _emails(_suggest(client, staff, "bucho")) == ["bucho@sasaki.example.jp"]
    # 届いたメールの相手と、送った宛先は同じ人なら1つにまとめて数える
    mid2 = _mail(tmp_path, "owner@example.jp", "オーナー")
    _replied(tmp_path, mid2, to="owner@example.jp")
    items = _suggest(client, staff, "owner")
    assert _emails(items) == ["owner@example.jp"]
    assert items[0]["name"] == "オーナー" and items[0]["count"] == 3


def test_history_excludes_own_and_form_senders(client, users, tmp_path):
    from server import db
    _, staff, admin = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_from", "株式会社インビックス <info@bics.example>")
        db.set_setting(conn, "mail_form_senders", "@form.example.jp")
    _mail(tmp_path, "info@bics.example", "自分たち")
    _mail(tmp_path, "wordpress@bics-site.example", "WordPress")
    _mail(tmp_path, "noreply@booking.example", "予約サイト")
    _mail(tmp_path, "info@form.example.jp", "フォーム")
    _mail(tmp_path, "info@customer.example.jp", "お客様")
    mid = _mail(tmp_path, "guest@example.jp")
    _replied(tmp_path, mid, cc="info@bics.example, Info@Customer.example.jp")
    assert _emails(_suggest(client, staff, "info")) == ["info@customer.example.jp"]
    for q in ("wordpress", "noreply", "自分たち", "フォーム"):
        assert _suggest(client, staff, q) == [], q
    # アドレス帳に自分で入れたものは出す (管理者が決めたので)
    _add(client, admin, "info@bics.example", "代表")
    assert _emails(_suggest(client, staff, "代表")) == ["info@bics.example"]


def test_book_and_history_do_not_repeat(client, users, tmp_path):
    _, staff, admin = users
    _mail(tmp_path, "taro@abc.example.jp", "Taro Yamada")
    _add(client, admin, "TARO@abc.example.jp", "山田 太郎")
    items = _suggest(client, staff, "taro")
    assert _emails(items) == ["TARO@abc.example.jp"]
    # メールに書かれた名前で探しても、アドレス帳の名前で出る
    items = _suggest(client, staff, "yamada")
    assert len(items) == 1 and items[0]["name"] == "山田 太郎" and items[0]["source"] == "book"


def test_history_matches_client_name(client, users, tmp_path):
    _, staff, admin = users
    client.post("/api/mail/clients", headers=auth(admin),
                json={"name": "ABC商事", "patterns": ["@abc.example.jp"]})
    _mail(tmp_path, "suzuki@abc.example.jp", "鈴木")
    _mail(tmp_path, "sato@tokyo.abc.example.jp", "佐藤")   # 下のドメインも
    _mail(tmp_path, "other@zzz.example.jp", "ほか")
    items = _suggest(client, staff, "ABC商事")
    assert sorted(_emails(items)) == ["sato@tokyo.abc.example.jp", "suzuki@abc.example.jp"]
    assert all(i["client"] == "ABC商事" for i in items)


def test_like_wildcards_are_literal(client, users, tmp_path):
    _, staff, _ = users
    _mail(tmp_path, "a_b@example.jp", "A")
    _mail(tmp_path, "axb@example.jp", "B")
    assert _emails(_suggest(client, staff, "a_b")) == ["a_b@example.jp"]
    assert _suggest(client, staff, "%") == []


def test_limit_and_bad_params(client, users, tmp_path):
    _, staff, admin = users
    for i in range(12):
        _add(client, admin, f"user{i:02d}@example.jp", f"ユーザー{i:02d}")
    assert len(_suggest(client, staff, "user")) == 8
    assert len(_suggest(client, staff, "user", limit=3)) == 3
    assert client.get("/api/mail/address-suggest", headers=auth(staff),
                      params={"q": "u", "limit": 0}).status_code == 400
    assert client.get("/api/mail/address-suggest", headers=auth(staff),
                      params={"q": "u", "limit": 99}).status_code == 400
    assert client.get("/api/mail/address-suggest", headers=auth(staff),
                      params={"q": "x" * 101}).status_code == 400


def test_mail_detail_says_if_counterpart_is_in_book(client, users, tmp_path):
    _, staff, admin = users
    mid = _mail(tmp_path, "taro@abc.example.jp", "山田")
    assert client.get(f"/api/mail/{mid}", headers=auth(staff)).json()[
        "contact_in_address_book"] is False
    _add(client, admin, "Taro@ABC.example.jp")
    assert client.get(f"/api/mail/{mid}", headers=auth(staff)).json()[
        "contact_in_address_book"] is True


# ---------- 速さ (索引だけで探す) ----------

def test_queries_use_indexes_only(client, users, tmp_path):
    """候補を探す SQL が、本文の入った表を読まずに索引だけで済む (何万通でも速い)."""
    from server import address_book, db
    _mail(tmp_path, "taro@abc.example.jp", "山田")
    plans = []
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        orig = conn.execute

        class Spy:
            def execute(self, sql, params=()):
                if "FROM mails" in sql:
                    plans.append(" ".join(r[3] for r in orig("EXPLAIN QUERY PLAN " + sql, params)))
                return orig(sql, params)

            def __getattr__(self, name):
                return getattr(conn, name)

        address_book.suggest(Spy(), "taro")
    assert len(plans) == 2, plans
    assert "COVERING INDEX idx_mails_clients" in plans[0], plans[0]
    assert "COVERING INDEX idx_mails_sent_to" in plans[1], plans[1]


def test_migration_is_additive_and_repeatable(tmp_path):
    from server import db
    path = tmp_path / "zatsumu.db"
    db.connect(path).close()
    conn = db.connect(path)   # 2回目も同じ (何も壊さない)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"address_book", "idx_address_book_email", "idx_mails_sent_to"} <= names
    conn.close()
