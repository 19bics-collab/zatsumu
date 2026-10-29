"""取引先 (取引先ごとに、届いたメールと送った返信をまとめて見る) のテスト.

- まとめ方: 会社のドメインはドメインごと・Gmail などの個人と予約サイトはアドレスごと・
  自分たち・自社のドメイン・フォームや自動送信の送信元は入れない・前の版が入れたメール
- 管理者が決めたもの (名前を付ける・まとめる・分ける・消す) の強さの順 (アドレス > ドメイン > 決まり)。
  変えてもメールは変わらない
- 探す・ページ分け・最後のやり取りの新しい順
- やり取り (届いたメールと送った返信) の中身と順番
- 使える人 (管理者・メール担当者は見られる・変えるのは管理者だけ・ほかの人・トークン無し)
- 操作の記録
- 列・索引を足す移行 (何度でも同じ・5万通でも速い)・前の版 (67b0e63) が新しい DB で動く
- 画面 (取引先の画面・返信画面の印・受信箱の名前・使い方の開け閉め)

ネットワークには出ない (IMAP は偽物に差し替える)。
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth
from tests.test_mail_attachments import FakeIMAP

ROOT = Path(__file__).resolve().parent.parent
OWN = "info@bics.co.jp"
WP = "wordpress@bics.co.jp"

FORM_BODY = """会社名：株式会社やまだ不動産
お名前：山田 花子
メールアドレス：hanako@yamada-fudosan.co.jp

メッセージ本文:
運営代行の料金を教えてください。"""


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
    """管理者・メール担当者・メール画面を使えない人 (差出人は info@bics.co.jp)."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        admin = db.create_user(conn, "黒須", is_admin=True)
        staff = db.create_user(conn, "田中")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        member = db.create_user(conn, "佐藤")
        db.set_setting(conn, "mail_from", OWN)
    return admin, staff, member


def _db(tmp_path):
    from server import db
    return db.get_db(tmp_path / "zatsumu.db")


_seq = [0]


def _mail(tmp_path, addr, name="", day=1, status="unhandled", reply=None, from_addr=None,
          via_form=0, company="", subject=None, body="本文", category=""):
    """相手を決めたメールを1通入れて番号を返す (reply は (本文, 送った日))."""
    _seq[0] += 1
    n = _seq[0]
    received = f"2026-08-{day:02d}T00:{n % 60:02d}:00+00:00"
    replied = f"2026-08-{reply[1]:02d}T09:{n % 60:02d}:00+00:00" if reply else None
    with _db(tmp_path) as conn:
        return conn.execute(
            "INSERT INTO mails (message_id, from_addr, from_name, subject, body, received_at,"
            " fetched_at, status, contact_addr, contact_name, contact_source, contact_via_form,"
            " contact_company, reply_body, replied_at, replied_by, category)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"<c{n}@x>", from_addr or addr, name, subject or f"件名{n}", body, received,
             received, "replied" if reply else status, addr, name,
             "body" if via_form else "from", via_form, company,
             reply[0] if reply else "", replied, "黒須" if reply else "", category)).lastrowid


def _list(client, user, **params):
    r = client.get("/api/mail/clients", headers=auth(user), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _keys(client, user, **params):
    return [c["key"] for c in _list(client, user, **params)["clients"]]


def _detail(client, user, key, **params):
    r = client.get(f"/api/mail/clients/{key}", headers=auth(user), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _mails_snapshot(tmp_path):
    with _db(tmp_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT * FROM mails ORDER BY id")]


# ---------- まとめ方 ----------

def test_company_domains_group_by_domain_personal_by_address(client, users, tmp_path):
    admin, _staff, _member = users
    _mail(tmp_path, "tanaka@abc.co.jp", "田中", day=1)
    _mail(tmp_path, "Suzuki@ABC.co.jp", "鈴木", day=3)
    _mail(tmp_path, "taro@gmail.com", "太郎", day=2)
    _mail(tmp_path, "hanako@gmail.com", "花子", day=4)
    _mail(tmp_path, "guest-123@guest.airbnb.com", "Airbnb ゲスト", day=5)
    _mail(tmp_path, "guest-456@guest.airbnb.com", "別のゲスト", day=6)
    _mail(tmp_path, "user@docomo.ne.jp", "携帯の人", day=7)
    out = _list(client, admin)
    by_key = {c["key"]: c for c in out["clients"]}
    assert set(by_key) == {"d:abc.co.jp", "a:taro@gmail.com", "a:hanako@gmail.com",
                           "a:guest-123@guest.airbnb.com", "a:guest-456@guest.airbnb.com",
                           "a:user@docomo.ne.jp"}
    abc = by_key["d:abc.co.jp"]
    assert (abc["kind"], abc["total"], abc["address_count"], abc["named"]) == ("company", 2, 2, False)
    # 大文字・小文字の違うアドレスは1つ。名前は一番新しいメールの相手の名前
    assert {a["addr"] for a in abc["addresses"]} == {"tanaka@abc.co.jp", "suzuki@abc.co.jp"}
    assert abc["name"] == "鈴木" and abc["domains"] == ["abc.co.jp"]
    assert by_key["a:taro@gmail.com"]["kind"] == "person"
    # 最後のやり取りの新しい順
    assert [c["key"] for c in out["clients"]][:2] == ["a:user@docomo.ne.jp",
                                                      "a:guest-456@guest.airbnb.com"]


def test_own_addresses_form_and_automated_senders_never_become_clients(client, users, tmp_path):
    admin, _staff, _member = users
    from server import db
    with _db(tmp_path) as conn:
        conn.execute("INSERT INTO mail_accounts (label, from_addr, imap_user, created_at)"
                     " VALUES ('黒須', 'kurosu@bics.co.jp', 'kurosu@bics.co.jp', 'now')")
        db.set_setting(conn, "mail_form_senders", "@forms.example.jp")
    _mail(tmp_path, "kurosu@bics.co.jp", "黒須")                    # 自分たち
    _mail(tmp_path, "sales@bics.co.jp", "社内の人")                 # 自社のドメイン
    _mail(tmp_path, "noreply@shop.example.com", "お知らせ")        # 自動送信
    _mail(tmp_path, "contact@forms.example.jp", "フォーム")         # 設定のフォームの送信元
    _mail(tmp_path, "", "中村", from_addr=WP, via_form=1)          # フォームでアドレスが無い
    _mail(tmp_path, "customer@example.org", "お客様", from_addr=WP, via_form=1)
    # 前の版が入れたメール (相手を決めていない) で、差出人がフォーム
    _add_mail(tmp_path, n=90, from_addr=WP, from_name="WordPress")
    assert _keys(client, admin) == ["d:example.org"]
    # 自分たちのドメイン・個人のドメインの番号は開けない
    for key in ("d:bics.co.jp", "d:gmail.com", "a:kurosu@bics.co.jp", "a:noreply@shop.example.com"):
        r = client.get(f"/api/mail/clients/{key}", headers=auth(admin))
        assert r.status_code == 404, key
    # Gmail で受信していても、Gmail の人は自社扱いにしない
    with _db(tmp_path) as conn:
        db.set_setting(conn, "mail_from", "mybiz@gmail.com")
        conn.execute("DELETE FROM mail_accounts")
    _mail(tmp_path, "friend@gmail.com", "友だち")
    _mail(tmp_path, "mybiz@gmail.com", "自分")
    assert "a:friend@gmail.com" in _keys(client, admin)
    assert "a:mybiz@gmail.com" not in _keys(client, admin)


def test_legacy_rows_use_sender_and_get_domain(client, users, tmp_path):
    """前の版が入れた (相手を決めていない) メールは差出人で、ドメインも DB が入れる."""
    admin, _staff, _member = users
    mid = _add_mail(tmp_path, n=1, from_addr="Old@Legacy.co.jp", from_name="前の人")
    with _db(tmp_path) as conn:
        row = conn.execute("SELECT contact_addr, contact_domain, contact_source FROM mails"
                           " WHERE id = ?", (mid,)).fetchone()
    assert tuple(row) == ("Old@Legacy.co.jp", "legacy.co.jp", "")
    out = _detail(client, admin, "d:legacy.co.jp")
    assert out["total"] == 1 and out["name"] == "前の人"
    # 相手を決め直すと (フォームの本文のお客様など)、ドメインもそろう
    with _db(tmp_path) as conn:
        conn.execute("UPDATE mails SET contact_addr = 'x@new.example.jp' WHERE id = ?", (mid,))
        assert conn.execute("SELECT contact_domain FROM mails WHERE id = ?",
                            (mid,)).fetchone()[0] == "new.example.jp"


def test_form_company_name_is_the_default_name(client, users, tmp_path, monkeypatch):
    """HPのフォームの「会社名」を受信したときに覚え、取引先の名前にする."""
    admin, _staff, _member = users
    from server import db, mail
    m = EmailMessage()
    m["From"] = f"WordPress <{WP}>"
    m["To"] = OWN
    m["Subject"] = "【HPお問い合わせ】"
    m["Message-ID"] = "<form1@x>"
    m["Date"] = "Mon, 03 Aug 2026 10:00:00 +0900"
    m.set_content(FORM_BODY)
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP({1: m.as_bytes()}))
    with _db(tmp_path) as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com", "mail_auto_draft": 0}
        assert len(mail.fetch_new_mail(conn, s)) == 1
        row = conn.execute("SELECT * FROM mails").fetchone()
    assert (row["contact_addr"], row["contact_company"], row["contact_domain"]) == \
        ("hanako@yamada-fudosan.co.jp", "株式会社やまだ不動産", "yamada-fudosan.co.jp")
    # 後から同じ会社の別の人がふつうのメールをくれても、名前はフォームの会社名
    _mail(tmp_path, "jiro@yamada-fudosan.co.jp", "山田 次郎", day=20)
    c = _list(client, admin)["clients"][0]
    assert (c["key"], c["name"], c["total"]) == ("d:yamada-fudosan.co.jp", "株式会社やまだ不動産", 2)
    assert mail.company_from_body("会社名（フリガナ）：ヤマダ\n貴社名：山田商店") == "山田商店"
    assert mail.company_from_body("お名前：山田") == ""


def test_fill_missing_companies_reads_old_form_mails_once(tmp_path):
    from server import db, mail
    conn = db.connect(tmp_path / "zatsumu.db")
    try:
        conn.execute("INSERT INTO mails (message_id, from_addr, body, received_at, fetched_at,"
                     " contact_addr, contact_source, contact_via_form) VALUES"
                     " ('<a>', ?, ?, 'x', 'x', 'hanako@yamada-fudosan.co.jp', 'body', 1)",
                     (WP, FORM_BODY))
        conn.execute("INSERT INTO mails (message_id, from_addr, body, received_at, fetched_at)"
                     " VALUES ('<b>', 'taro@example.com', '会社名: 関係ない', 'x', 'x')")
        conn.commit()
        assert mail.fill_missing_companies(conn) == 1
        assert mail.fill_missing_companies(conn) == 0    # 2回目は何もしない
        assert [r[0] for r in conn.execute("SELECT contact_company FROM mails ORDER BY id")] == \
            ["株式会社やまだ不動産", ""]
    finally:
        conn.close()


# ---------- 管理者が決めたもの (名前・まとめる・分ける・消す) ----------

def test_rename_merge_split_delete_precedence(client, users, tmp_path):
    admin, _staff, _member = users
    _mail(tmp_path, "tanaka@abc.co.jp", "田中", day=1)
    _mail(tmp_path, "suzuki@abc.co.jp", "鈴木", day=2)
    _mail(tmp_path, "boss@abc-group.jp", "社長", day=3)
    _mail(tmp_path, "taro@gmail.com", "太郎", day=4)
    before = _mails_snapshot(tmp_path)
    h = auth(admin)
    # 名前を付ける (d:abc.co.jp のまとめ方を引き継ぐ)
    r = client.post("/api/mail/clients", headers=h,
                    json={"name": " ABC  不動産 ", "note": "窓口は田中さん", "from_key": "d:abc.co.jp"})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert r.json()["patterns"] == ["@abc.co.jp"] and r.json()["name"] == "ABC 不動産"
    # 古い番号でも今の取引先が開ける
    d = _detail(client, admin, "d:abc.co.jp")
    assert (d["key"], d["name"], d["named"], d["note"], d["total"]) == \
        (key, "ABC 不動産", True, "窓口は田中さん", 2)
    assert "d:abc.co.jp" not in _keys(client, admin)
    # まとめる: 別のドメインと、Gmail の人
    assert client.post(f"/api/mail/clients/{key}/rules", headers=h,
                       json={"pattern": "abc-group.jp"}).json()["pattern"] == "@abc-group.jp"
    assert client.post(f"/api/mail/clients/{key}/rules", headers=h,
                       json={"from_key": "a:taro@gmail.com"}).status_code == 200
    d = _detail(client, admin, key)
    assert d["total"] == 4 and d["address_count"] == 4
    assert _keys(client, admin) == [key]
    # 分ける: アドレスで決めたものはドメインで決めたものより強い。ほかの取引先にドメインで
    # 入っている人は、移すと言わなければ 409 (黙ってその取引先から抜けない)
    r = client.post("/api/mail/clients", headers=h,
                    json={"name": "鈴木さん", "patterns": ["Suzuki@ABC.co.jp"]})
    assert r.status_code == 409 and "ABC 不動産" in r.json()["detail"]
    assert "「@abc.co.jp」でまとめている" in r.json()["detail"]
    r = client.post("/api/mail/clients", headers=h,
                    json={"name": "鈴木さん", "patterns": ["Suzuki@ABC.co.jp"], "move": True})
    assert r.status_code == 200, r.text
    suzuki = r.json()["key"]
    assert _detail(client, admin, suzuki)["total"] == 1
    assert _detail(client, admin, key)["total"] == 3
    # 太郎さんを鈴木さんの方へ移す: 移すと言わなければ 409
    r = client.post(f"/api/mail/clients/{suzuki}/rules", headers=h, json={"pattern": "taro@gmail.com"})
    assert r.status_code == 409 and "ABC 不動産" in r.json()["detail"]
    r = client.post(f"/api/mail/clients/{suzuki}/rules", headers=h,
                    json={"pattern": "taro@gmail.com", "move": True})
    assert r.status_code == 200 and r.json()["moved_from"] == "ABC 不動産"
    assert _detail(client, admin, suzuki)["total"] == 2
    # 外す: 元の分け方に戻る
    rule = next(x for x in _detail(client, admin, suzuki)["rules"] if x["pattern"] == "taro@gmail.com")
    assert client.delete(f"/api/mail/clients/{suzuki}/rules/{rule['id']}", headers=h).status_code == 200
    assert "a:taro@gmail.com" in _keys(client, admin)
    # 消す: ABC 不動産のメールは決まった分け方に戻る (鈴木さんの分けたものは残る)
    assert client.delete(f"/api/mail/clients/{key}", headers=h).status_code == 200
    assert set(_keys(client, admin)) == {suzuki, "d:abc.co.jp", "d:abc-group.jp", "a:taro@gmail.com"}
    assert _detail(client, admin, "d:abc.co.jp")["total"] == 1
    assert client.get(f"/api/mail/clients/{key}", headers=h).status_code == 404
    with _db(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM client_rules WHERE client_id = ?",
                            (int(key[2:]),)).fetchone()[0] == 0
    # メールは一度も変えていない
    assert _mails_snapshot(tmp_path) == before


def test_rename_and_note_and_validation(client, users, tmp_path):
    admin, _staff, _member = users
    _mail(tmp_path, "taro@gmail.com", "太郎")
    h = auth(admin)
    key = client.post("/api/mail/clients", headers=h,
                      json={"name": "太郎さん", "from_key": "a:taro@gmail.com"}).json()["key"]
    r = client.patch(f"/api/mail/clients/{key}", headers=h, json={"name": "山田 太郎", "note": "  VIP \r\n"})
    assert r.status_code == 200 and r.json()["name"] == "山田 太郎" and r.json()["note"] == "VIP"
    assert _detail(client, admin, key)["kind"] == "person"
    bad = [
        ("POST", "/api/mail/clients", {"name": "  "}, 400, "名前"),
        ("POST", "/api/mail/clients", {"name": "x" * 61}, 400, "60文字"),
        ("PATCH", f"/api/mail/clients/{key}", {"note": "x" * 2001}, 400, "2000文字"),
        ("PATCH", f"/api/mail/clients/{key}", {}, 400, "変更内容"),
        ("PATCH", "/api/mail/clients/d:abc.co.jp", {"name": "x"}, 400, "名前を付けていない"),
        ("PATCH", "/api/mail/clients/c:999", {"name": "x"}, 404, "削除"),
        ("POST", f"/api/mail/clients/{key}/rules", {"pattern": "@gmail.com"}, 400, "個人のメール"),
        ("POST", f"/api/mail/clients/{key}/rules", {"pattern": "@bics.co.jp"}, 400, "自分たち"),
        ("POST", f"/api/mail/clients/{key}/rules", {"pattern": OWN}, 400, "自分たち"),
        ("POST", f"/api/mail/clients/{key}/rules", {"pattern": "noreply@shop.jp"}, 400, "送信元"),
        ("POST", f"/api/mail/clients/{key}/rules", {"pattern": "not an address"}, 400, "正しくありません"),
        ("POST", f"/api/mail/clients/{key}/rules", {"pattern": "taro@gmail.com"}, 409, "この取引先"),
        ("POST", f"/api/mail/clients/{key}/rules", {"from_key": key}, 400, "名前を付けた"),
        ("DELETE", f"/api/mail/clients/{key}/rules/999", None, 404, "入っていません"),
        ("GET", "/api/mail/clients/zz", None, 404, "見つかりません"),
        ("GET", "/api/mail/clients/a:~zz", None, 404, "見つかりません"),
        ("GET", "/api/mail/clients", {"limit": 0}, 400, "limit"),
    ]
    for method, path, body, code, text in bad:
        kw = {"params": body} if method == "GET" else ({"json": body} if body is not None else {})
        r = client.request(method, path, headers=h, **kw)
        assert r.status_code == code, (path, body, r.text)
        assert text in r.json()["detail"], (path, r.json())


def test_key_encoding_round_trip(client, users, tmp_path):
    admin, _staff, _member = users
    from server import clients
    for addr in ("o'brien/sales@gmail.com", "a~b%c#d?@gmail.com", "plain.name+tag@gmail.com"):
        key = clients.address_key(addr)
        assert all(c.isascii() and (c.isalnum() or c in ":._+-@~") for c in key)
        assert clients.parse_key(key) == ("a", addr)
    _mail(tmp_path, "o'brien/sales@gmail.com", "オブライエン")
    key = _keys(client, admin)[0]
    assert key == "a:o~27brien~2fsales@gmail.com"
    assert _detail(client, admin, key)["total"] == 1


# ---------- 探す・ページ分け ----------

def test_search_and_pagination(client, users, tmp_path):
    admin, _staff, _member = users
    for i in range(7):
        _mail(tmp_path, f"p{i}@co{i}.example.jp", f"担当{i}", day=1 + i)
    # 返信を送った日も「最後のやり取り」(返信が一番新しい)
    _mail(tmp_path, "old@zeta.example.jp", "ゼータ", day=1, reply=("返事", 28))
    keys = _keys(client, admin)
    assert keys[0] == "d:zeta.example.jp" and keys[1] == "d:co6.example.jp"
    page1 = _list(client, admin, limit=3)
    page2 = _list(client, admin, limit=3, offset=3)
    assert page1["total"] == page2["total"] == 8
    assert [c["key"] for c in page1["clients"] + page2["clients"]] == keys[:6]
    # 名前・アドレス・ドメインで探す (大文字・全角でも)
    assert _keys(client, admin, q="担当3") == ["d:co3.example.jp"]
    assert _keys(client, admin, q="P5@") == ["d:co5.example.jp"]
    assert _keys(client, admin, q="ｚｅｔａ") == ["d:zeta.example.jp"]
    assert _list(client, admin, q="ない言葉")["total"] == 0
    # 名前を付けた取引先は名前で探せ、メールが無くても一覧に出る
    client.post("/api/mail/clients", headers=auth(admin), json={"name": "まだメールの無い会社"})
    assert _list(client, admin, q="まだメール")["clients"][0]["total"] == 0
    assert [c["name"] for c in _list(client, admin, named=True)["clients"]] == ["まだメールの無い会社"]


# ---------- やり取り ----------

def test_timeline_mixes_received_and_replies_newest_first(client, users, tmp_path):
    admin, _staff, _member = users
    a = _mail(tmp_path, "tanaka@abc.co.jp", "田中", day=1, subject="最初の相談",
              reply=("田中様 お返事です", 2), category="清掃のご依頼")
    b = _mail(tmp_path, "suzuki@abc.co.jp", "鈴木", day=5, subject="請求書", body="請求書を" * 100,
              category="請求・お支払い")
    c = _mail(tmp_path, "tanaka@abc.co.jp", "田中", day=3, subject="追加の相談",
              reply=("追加のお返事", 6), category="清掃のご依頼")
    _mail(tmp_path, "other@else.co.jp", "ほか", day=4)
    d = _detail(client, admin, "d:abc.co.jp")
    assert [(t["kind"], t["mail_id"]) for t in d["timeline"]] == [
        ("out", c), ("in", b), ("in", c), ("out", a), ("in", a)]
    assert d["timeline_total"] == 5 and d["total"] == 3 and d["replies"] == 2
    assert (d["first_at"][:10], d["last_at"][:10]) == ("2026-08-01", "2026-08-06")
    assert d["unhandled"] == 1
    assert {x["category"]: x["count"] for x in d["categories"]} == \
        {"清掃のご依頼": 2, "請求・お支払い": 1}
    out = d["timeline"][0]
    assert (out["subject"], out["snippet"], out["replied_by"], out["sent_to"]) == \
        ("Re: 追加の相談", "追加のお返事", "黒須", "tanaka@abc.co.jp")
    inb = d["timeline"][1]
    assert inb["subject"] == "請求書" and inb["snippet"].endswith("…") and len(inb["snippet"]) <= 121
    assert (inb["contact_name"], inb["contact_addr"], inb["status"]) == ("鈴木", "suzuki@abc.co.jp",
                                                                          "unhandled")
    # ページ分け
    p2 = _detail(client, admin, "d:abc.co.jp", offset=2, limit=2)
    assert [(t["kind"], t["mail_id"]) for t in p2["timeline"]] == [("in", c), ("out", a)]
    # 全文は今までのメールの詳しい画面で読む
    full = client.get(f"/api/mail/{b}?thread=false", headers=auth(admin)).json()
    assert full["body"] == "請求書を" * 100


def test_mail_detail_and_inbox_show_client(client, users, tmp_path):
    admin, staff, _member = users
    a = _mail(tmp_path, "tanaka@abc.co.jp", "田中")
    g = _mail(tmp_path, "taro@gmail.com", "太郎")
    own = _mail(tmp_path, "sales@bics.co.jp", "社内")
    j = _mail(tmp_path, "jiro@gmail.com", "次郎")
    assert client.get(f"/api/mail/{a}", headers=auth(staff)).json()["client"] == \
        {"key": "d:abc.co.jp", "name": "田中", "named": False, "kind": "company"}
    # Gmail などの個人は「個人」(返信画面の印の絵が 👤 になる)
    assert client.get(f"/api/mail/{g}", headers=auth(staff)).json()["client"] == \
        {"key": "a:taro@gmail.com", "name": "太郎", "named": False, "kind": "person"}
    assert client.get(f"/api/mail/{own}", headers=auth(staff)).json()["client"] is None
    key = client.post("/api/mail/clients", headers=auth(admin),
                      json={"name": "ABC", "from_key": "d:abc.co.jp"}).json()["key"]
    assert client.get(f"/api/mail/{a}", headers=auth(staff)).json()["client"] == \
        {"key": key, "name": "ABC", "named": True, "kind": "company"}
    # 名前を付けた個人 (アドレスだけの取引先) は「個人」
    pkey = client.post("/api/mail/clients", headers=auth(admin),
                       json={"name": "太郎さん", "from_key": "a:taro@gmail.com"}).json()["key"]
    assert client.get(f"/api/mail/{g}", headers=auth(staff)).json()["client"] == \
        {"key": pkey, "name": "太郎さん", "named": True, "kind": "person"}
    rows = {m["id"]: m for m in client.get("/api/mail", headers=auth(staff)).json()}
    assert (rows[a]["client_name"], rows[a]["client_key"], rows[a]["client_kind"]) == \
        ("ABC", key, "company")
    assert (rows[g]["client_name"], rows[g]["client_kind"]) == ("太郎さん", "person")
    # 名前を付けていない取引先・取引先にしない相手には出さない
    assert rows[j]["client_name"] == "" and rows[own]["client_name"] == ""
    assert rows[j]["client_kind"] == "" and rows[own]["client_kind"] == ""


# ---------- 使える人・操作の記録 ----------

def test_permissions(client, users, tmp_path):
    admin, staff, member = users
    _mail(tmp_path, "tanaka@abc.co.jp", "田中")
    key = client.post("/api/mail/clients", headers=auth(admin),
                      json={"name": "ABC", "from_key": "d:abc.co.jp"}).json()["key"]
    # メール担当者は見られる
    assert _keys(client, staff) == [key]
    assert _detail(client, staff, key)["name"] == "ABC"
    writes = [("POST", "/api/mail/clients", {"name": "x"}),
              ("PATCH", f"/api/mail/clients/{key}", {"name": "x"}),
              ("DELETE", f"/api/mail/clients/{key}", None),
              ("POST", f"/api/mail/clients/{key}/rules", {"pattern": "a@b.co.jp"}),
              ("DELETE", f"/api/mail/clients/{key}/rules/1", None)]
    for method, path, body in writes:
        kw = {"json": body} if body else {}
        assert client.request(method, path, headers=auth(staff), **kw).status_code == 403, path
        assert client.request(method, path, headers=auth(member), **kw).status_code == 403, path
        assert client.request(method, path, **kw).status_code == 401, path
    for path in ("/api/mail/clients", f"/api/mail/clients/{key}"):
        r = client.get(path, headers=auth(member))
        assert r.status_code == 403 and r.json()["detail"] == "mail_access_required"
        assert client.get(path).status_code == 401
    # 何も変わっていない
    assert _detail(client, admin, key)["name"] == "ABC"


def test_edits_are_audit_logged(client, users, tmp_path):
    admin, _staff, _member = users
    _mail(tmp_path, "tanaka@abc.co.jp", "田中")
    h = auth(admin)
    key = client.post("/api/mail/clients", headers=h,
                      json={"name": "ABC", "from_key": "d:abc.co.jp"}).json()["key"]
    client.patch(f"/api/mail/clients/{key}", headers=h, json={"note": "メモ"})
    rid = client.post(f"/api/mail/clients/{key}/rules", headers=h,
                      json={"pattern": "x@other.co.jp"}).json()["rule_id"]
    client.delete(f"/api/mail/clients/{key}/rules/{rid}", headers=h)
    client.delete(f"/api/mail/clients/{key}", headers=h)
    with _db(tmp_path) as conn:
        rows = [(r["admin_id"], r["action"], r["detail"]) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action LIKE 'client%' ORDER BY id")]
    cid = key[2:]
    assert rows == [
        (admin["id"], "client_create", f"client={cid} name=ABC patterns=@abc.co.jp"),
        (admin["id"], "client_update", f"client={cid} note=メモ"),
        (admin["id"], "client_rule_add", f"client={cid} pattern=x@other.co.jp"),
        (admin["id"], "client_rule_delete", f"client={cid} pattern=x@other.co.jp"),
        (admin["id"], "client_delete", f"client={cid} name=ABC patterns=@abc.co.jp"),
    ]


# ---------- 見直しで見つかったこと ----------

def test_free_mail_providers_and_relays_are_one_person_each(client, users, tmp_path):
    """プロバイダ・海外のフリーメール・Apple の中継は、同じドメインでも別々のお客様."""
    admin, _staff, _member = users
    pairs = [("guestA@eonet.ne.jp", "guestB@eonet.ne.jp"), ("li@126.com", "wang@126.com"),
             ("kim@hanmail.net", "park@hanmail.net"), ("a@foxmail.com", "b@foxmail.com"),
             ("ivan@yandex.ru", "olga@mail.ru"), ("hans@gmx.de", "anna@web.de"),
             ("x1@privaterelay.appleid.com", "x2@privaterelay.appleid.com"),
             ("p@odn.ne.jp", "q@commufa.jp")]
    for a, b in pairs:
        _mail(tmp_path, a)
        _mail(tmp_path, b)
    keys = _keys(client, admin)
    assert sorted(keys) == sorted("a:" + x.lower() for pair in pairs for x in pair)
    assert _detail(client, admin, "a:guesta@eonet.ne.jp")["total"] == 1
    # 一覧は1か所 (受信の「自社のドメイン」「同じ会社」の見分けも同じ一覧を使う)
    from server import db, mail
    assert mail._FREE_MAIL_DOMAINS == frozenset(db.FREE_MAIL_DOMAINS)
    assert set(db.FREE_MAIL_DOMAINS) <= set(db.DEFAULT_PERSONAL_DOMAINS)
    assert len(set(db.DEFAULT_PERSONAL_DOMAINS)) == len(db.DEFAULT_PERSONAL_DOMAINS)
    from server import clients
    assert clients.clean_personal_domains("\n".join(db.DEFAULT_PERSONAL_DOMAINS)) == \
        "\n".join(db.DEFAULT_PERSONAL_DOMAINS)
    assert not mail._same_org("a@eonet.ne.jp", "b@eonet.ne.jp")
    # 自分たちが eonet.ne.jp で受信していても、eonet の人を自社の人とはみなさない
    rules = mail.ContactRules({"mail_from": "shop@eonet.ne.jp"})
    assert "eonet.ne.jp" not in rules.own_domains


def _set_dates(tmp_path, mail_id, received, fetched):
    with _db(tmp_path) as conn:
        conn.execute("UPDATE mails SET received_at = ?, fetched_at = ? WHERE id = ?",
                     (received, fetched, mail_id))


def test_future_dates_count_from_when_the_mail_arrived(client, users, tmp_path):
    """差出人が先の日付を付けたメールは、取り込んだ時刻で並べる (一覧・やり取りの一番上に居座らない)."""
    admin, _staff, _member = users
    spam = _mail(tmp_path, "win@lucky-prize.biz", "当選", subject="当選しました")
    _set_dates(tmp_path, spam, "2099-01-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00")
    early = _mail(tmp_path, "tanaka@abc.co.jp", "田中", subject="先の日付")
    _set_dates(tmp_path, early, "2027-06-01T00:00:00+00:00", "2026-08-03T00:00:00+00:00")
    later = _mail(tmp_path, "suzuki@abc.co.jp", "鈴木", subject="新しいメール")
    _set_dates(tmp_path, later, "2026-08-20T00:00:00+00:00", "2026-08-20T00:01:00+00:00")
    _mail(tmp_path, "boss@other.co.jp", "社長", day=10)
    out = _list(client, admin)
    assert [c["key"] for c in out["clients"]] == ["d:abc.co.jp", "d:other.co.jp", "d:lucky-prize.biz"]
    by_key = {c["key"]: c for c in out["clients"]}
    assert by_key["d:lucky-prize.biz"]["last_at"] == "2026-08-02T00:00:00+00:00"
    abc = _detail(client, admin, "d:abc.co.jp")
    assert abc["last_at"] == "2026-08-20T00:00:00+00:00"
    assert abc["first_at"] == "2026-08-03T00:00:00+00:00"
    # 名前も、取り込んだ時刻で一番新しいメールの人
    assert abc["name"] == "鈴木"
    # やり取りの順番も取り込んだ時刻 (出す日時は差出人が付けた日付のまま)
    assert [(t["mail_id"], t["at"]) for t in abc["timeline"]] == \
        [(later, "2026-08-20T00:00:00+00:00"), (early, "2027-06-01T00:00:00+00:00")]


def test_staff_personal_addresses_are_not_clients(client, users, tmp_path):
    """登録している人 (メール担当者など) の Gmail などのアドレスは、取引先にしない."""
    admin, staff, _member = users
    with _db(tmp_path) as conn:
        conn.execute("UPDATE users SET email = 'Tanaka.Staff@gmail.com' WHERE id = ?", (staff["id"],))
    _mail(tmp_path, "tanaka.staff@gmail.com", "田中（スタッフ）")
    _mail(tmp_path, "taro@gmail.com", "太郎")
    assert _keys(client, admin) == ["a:taro@gmail.com"]
    assert client.get("/api/mail/clients/a:tanaka.staff@gmail.com",
                      headers=auth(admin)).status_code == 404
    key = client.post("/api/mail/clients", headers=auth(admin),
                      json={"name": "太郎さん", "from_key": "a:taro@gmail.com"}).json()["key"]
    r = client.post(f"/api/mail/clients/{key}/rules", headers=auth(admin),
                    json={"pattern": "tanaka.staff@gmail.com"})
    assert r.status_code == 400 and "社内の人" in r.json()["detail"]


def test_domain_rules_cover_subdomains(client, users, tmp_path):
    """「@abc.co.jp」は下のドメイン (tokyo.abc.co.jp) も入れる。一番長く合う決まりが強い."""
    admin, _staff, _member = users
    _mail(tmp_path, "a@tokyo.abc.co.jp", "東京", day=1)
    _mail(tmp_path, "b@osaka.abc.co.jp", "大阪", day=2)
    _mail(tmp_path, "c@abc.co.jp", "本社", day=3)
    _mail(tmp_path, "d@xabc.co.jp", "別の会社", day=4)
    h = auth(admin)
    # 決めていなければ、ドメインごと
    assert set(_keys(client, admin)) == {"d:tokyo.abc.co.jp", "d:osaka.abc.co.jp",
                                         "d:abc.co.jp", "d:xabc.co.jp"}
    abc = client.post("/api/mail/clients", headers=h,
                      json={"name": "ABC", "from_key": "d:abc.co.jp"}).json()["key"]
    assert set(_keys(client, admin)) == {abc, "d:xabc.co.jp"}
    d = _detail(client, admin, abc)
    assert (d["total"], d["address_count"]) == (3, 3)
    assert {t["contact_addr"] for t in d["timeline"]} == \
        {"a@tokyo.abc.co.jp", "b@osaka.abc.co.jp", "c@abc.co.jp"}
    # 下のドメインの古い番号でも、今の取引先が開ける
    assert _detail(client, admin, "d:tokyo.abc.co.jp")["key"] == abc
    # 下のドメインだけを別の取引先に: 今は ABC に入っているので、移すと言わなければ 409
    r = client.post("/api/mail/clients", headers=h, json={"name": "ABC 東京", "patterns": ["@tokyo.abc.co.jp"]})
    assert r.status_code == 409 and "ABC" in r.json()["detail"]
    r = client.post("/api/mail/clients", headers=h,
                    json={"name": "ABC 東京", "patterns": ["@tokyo.abc.co.jp"], "move": True})
    assert r.status_code == 200, r.text
    tokyo = r.json()["key"]
    assert _detail(client, admin, tokyo)["total"] == 1
    assert _detail(client, admin, abc)["total"] == 2
    assert {c["key"]: c["total"] for c in _list(client, admin)["clients"]} == \
        {abc: 2, tokyo: 1, "d:xabc.co.jp": 1}
    # 広すぎるドメインではまとめられない (別々の会社が全部入ってしまう)
    for bad in ("@co.jp", "ne.jp", "@com.cn", "@co.uk", "jp", "@com"):
        r = client.post(f"/api/mail/clients/{abc}/rules", headers=h, json={"pattern": bad})
        assert r.status_code == 400, bad
        assert ("広すぎ" if "." in bad else "正しくありません") in r.json()["detail"], bad
    # 消すと、下のドメインも元の分け方に戻る
    assert client.delete(f"/api/mail/clients/{abc}", headers=h).status_code == 200
    assert set(_keys(client, admin)) == {tokyo, "d:osaka.abc.co.jp", "d:abc.co.jp", "d:xabc.co.jp"}


def test_keys_without_mail_are_not_found(client, users, tmp_path):
    """メールの無いドメイン・アドレスの番号は 404 (空の取引先を出さない・意味の無い決まりを作らない)."""
    admin, _staff, _member = users
    _mail(tmp_path, "tanaka@abc.co.jp", "田中")
    _mail(tmp_path, "taro@gmail.com", "太郎")
    h = auth(admin)
    for key in ("d:nonexistent.example", "d:co.jp", "d:jp", "d:x@y", "d:abc.co.jp@x",
                "a:nobody@nowhere.example", "a:hanako@gmail.com", "a:nobody@abc.co.jp~"):
        r = client.get(f"/api/mail/clients/{key}", headers=h)
        assert r.status_code == 404, key
        assert client.post("/api/mail/clients", headers=h,
                           json={"name": "x", "from_key": key}).status_code == 404, key
    # メールのある相手は開ける (アドレスの番号で同じ会社の人も)
    assert _detail(client, admin, "a:tanaka@abc.co.jp")["key"] == "d:abc.co.jp"
    assert _detail(client, admin, "a:taro@gmail.com")["total"] == 1
    # 会社の人を全員ほかの取引先に入れると、ドメインの番号は見つからない
    client.post("/api/mail/clients", headers=h, json={"name": "田中さん", "patterns": ["tanaka@abc.co.jp"]})
    assert client.get("/api/mail/clients/d:abc.co.jp", headers=h).status_code == 404
    with _db(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1


def test_adding_an_address_from_another_named_client_asks_first(client, users, tmp_path):
    """ほかの取引先にドメインで入っている人のアドレスを足すときは、確かめてから移し、記録に残す."""
    admin, _staff, _member = users
    _mail(tmp_path, "tanaka@sakura.co.jp", "田中", reply=("返信", 3))
    _mail(tmp_path, "yamada@sakura.co.jp", "山田")
    _mail(tmp_path, "sasaki@sasaki.co.jp", "佐々木")
    h = auth(admin)
    sakura = client.post("/api/mail/clients", headers=h,
                         json={"name": "さくら不動産", "from_key": "d:sakura.co.jp"}).json()["key"]
    sasaki = client.post("/api/mail/clients", headers=h,
                         json={"name": "佐々木商事", "from_key": "d:sasaki.co.jp"}).json()["key"]
    r = client.post(f"/api/mail/clients/{sasaki}/rules", headers=h,
                    json={"pattern": "tanaka@sakura.co.jp"})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("「tanaka@sakura.co.jp」は今、取引先「さくら不動産」に入っています")
    assert "（こちらに移すときは「移す」を選んでください）" in r.json()["detail"]
    # 何も変わっていない
    assert _detail(client, admin, sakura)["total"] == 2
    assert _detail(client, admin, sasaki)["total"] == 1
    r = client.post(f"/api/mail/clients/{sasaki}/rules", headers=h,
                    json={"pattern": "tanaka@sakura.co.jp", "move": True})
    assert r.status_code == 200 and r.json()["moved_from"] == "さくら不動産"
    assert _detail(client, admin, sakura)["total"] == 1
    assert _detail(client, admin, sasaki)["total"] == 2
    with _db(tmp_path) as conn:
        detail = conn.execute("SELECT detail FROM audit_log WHERE action = 'client_rule_add'"
                              " ORDER BY id DESC").fetchone()[0]
    assert detail == f"client={sasaki[2:]} pattern=tanaka@sakura.co.jp moved_from=さくら不動産"
    # 同じ取引先にドメインで入っている人のアドレスは、そのまま足せる
    r = client.post(f"/api/mail/clients/{sakura}/rules", headers=h, json={"pattern": "yamada@sakura.co.jp"})
    assert r.status_code == 200 and r.json()["moved_from"] is None


def test_client_names_must_be_distinct(client, users, tmp_path):
    """同じ名前の取引先は作れない (「まとめる先」の選択肢で、どれのことか分からなくならないように)."""
    admin, _staff, _member = users
    _mail(tmp_path, "a@sakura.co.jp")
    _mail(tmp_path, "b@sasaki.co.jp")
    _mail(tmp_path, "c@abc.co.jp")
    h = auth(admin)
    sakura = client.post("/api/mail/clients", headers=h,
                         json={"name": "さくら不動産 ABC", "from_key": "d:sakura.co.jp"}).json()["key"]
    sasaki = client.post("/api/mail/clients", headers=h,
                         json={"name": "佐々木商事", "from_key": "d:sasaki.co.jp"}).json()["key"]
    # 全角・半角、大文字・小文字、空白の数の違いも同じ名前
    for name in ("さくら不動産 ABC", "さくら不動産　ＡＢＣ", " さくら不動産  abc "):
        r = client.patch(f"/api/mail/clients/{sasaki}", headers=h, json={"name": name})
        assert r.status_code == 409 and "同じ名前の取引先「さくら不動産 ABC」" in r.json()["detail"], name
        r = client.post("/api/mail/clients", headers=h, json={"name": name, "from_key": "d:abc.co.jp"})
        assert r.status_code == 409, name
    assert _detail(client, admin, sasaki)["name"] == "佐々木商事"
    with _db(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 2
    # 自分の名前のまま保存するのはよい
    assert client.patch(f"/api/mail/clients/{sakura}", headers=h,
                        json={"name": "さくら不動産　ＡＢＣ"}).status_code == 200
    # 選択肢でも見分けられるように、一覧にドメインを返している
    named = _list(client, admin, named="true")["clients"]
    assert {c["name"]: c["domains"] for c in named} == \
        {"さくら不動産 ABC": ["sakura.co.jp"], "佐々木商事": ["sasaki.co.jp"]}


# ---------- 設定「1人ずつに分けるドメイン」 ----------

def test_personal_domains_setting(client, users, tmp_path):
    admin, _staff, _member = users
    _mail(tmp_path, "a@freemail.example.jp", "Aさん")
    _mail(tmp_path, "b@freemail.example.jp", "Bさん")
    assert _keys(client, admin) == ["d:freemail.example.jp"]
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert "gmail.com" in s["mail_client_personal_domains"].split("\n")
    assert s["mail_client_personal_domains_default"] == s["mail_client_personal_domains"]
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_client_personal_domains": "gmail.com\n@FreeMail.example.jp, gmail.com"})
    assert r.status_code == 200, r.text
    assert r.json()["mail_client_personal_domains"] == "gmail.com\nfreemail.example.jp"
    assert sorted(_keys(client, admin)) == ["a:a@freemail.example.jp", "a:b@freemail.example.jp"]
    for bad, text in (("", "1つ以上"), ("not_a domain!", "正しくありません")):
        r = client.patch("/api/settings", headers=auth(admin),
                         json={"mail_client_personal_domains": bad})
        assert r.status_code == 400 and text in r.json()["detail"]


# ---------- 移行・前の版 ----------

def _old_code_dir(tmp_path, rev="67b0e63"):
    """本番が今動いている版のコードを取り出す (無ければテストを飛ばす)."""
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


def test_migration_from_previous_version_is_fast_and_idempotent(tmp_path):
    """前の版の DB (5万通) を開くと、列・トリガー・索引が足され、ドメインがすぐ入る (2回目は何もしない)."""
    old_dir = _old_code_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    out = _run_old(old_dir, data, f"""
        from server import db
        conn = db.connect({str(data / "zatsumu.db")!r})
        rows = [(f"<m{{i}}@x>", f"user{{i % 3000}}@co{{i % 700}}.example.jp", "名前", "本文" * 200,
                 "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00") for i in range(50000)]
        conn.executemany("INSERT INTO mails (message_id, from_addr, from_name, body, received_at,"
                         " fetched_at) VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mails)")]
        print(json.dumps({{"has_domain": "contact_domain" in cols}}))
    """)
    assert out == {"has_domain": False}
    from server import clients, db
    t = time.time()
    conn = db.connect(data / "zatsumu.db")
    first = time.time() - t
    try:
        assert first < 20, first
        assert conn.execute("SELECT COUNT(*) FROM mails WHERE contact_domain = ''").fetchone()[0] == 0
        assert conn.execute("SELECT contact_domain FROM mails WHERE id = 2").fetchone()[0] == \
            "co1.example.jp"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"clients", "client_rules", "idx_mails_clients", "trg_mails_contact_domain_ins",
                "trg_mails_contact_domain_upd"} <= names
        # 取引先の一覧は索引だけで数える (表の本文を読まない)
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN " + clients._STATS_SQL
            + " WHERE contact_addr != '' GROUP BY contact_domain, contact_addr"))
        assert "COVERING INDEX idx_mails_clients" in plan, plan
        t = time.time()
        out = clients.list_clients(conn)
        assert out["total"] == 700 and time.time() - t < 10
        t = time.time()
        d = clients.client_detail(conn, "d:co5.example.jp")
        assert d["total"] == 72 and len(d["timeline"]) == 30 and time.time() - t < 2
        # 名前を付けた取引先 (ドメインの決まりは下のドメインにも当てる) も速い
        out = clients.create_client(conn, clients.Grouping(conn), "会社5", "", ["@co5.example.jp"])
        t = time.time()
        d = clients.client_detail(conn, out["key"])
        assert d["total"] == 72 and len(d["timeline"]) == 30 and time.time() - t < 2
        conn.rollback()
    finally:
        conn.close()
    # 2回目に開いても同じ (もう一度書き込まない)
    t = time.time()
    conn = db.connect(data / "zatsumu.db")
    try:
        assert time.time() - t < 2
        assert conn.total_changes == 0
    finally:
        conn.close()


def test_clients_index_without_fetched_at_is_rebuilt(tmp_path):
    """開発中の版で作った索引 (取り込んだ時刻が無い) は、開いたときに作り直す (何度開いても同じ)."""
    from server import clients, db
    path = tmp_path / "zatsumu.db"
    conn = db.connect(path)
    conn.execute("DROP INDEX idx_mails_clients")
    conn.execute("CREATE INDEX idx_mails_clients ON mails(contact_domain, contact_addr,"
                 " received_at, status, replied_at, contact_name, contact_company)")
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = db.connect(path)
        try:
            cols = [r[2] for r in conn.execute("PRAGMA index_info(idx_mails_clients)")]
            assert "fetched_at" in cols
            plan = " ".join(str(tuple(r)) for r in conn.execute(
                "EXPLAIN QUERY PLAN " + clients._STATS_SQL
                + " WHERE contact_addr != '' GROUP BY contact_domain, contact_addr"))
            assert "COVERING INDEX idx_mails_clients" in plan, plan
        finally:
            conn.close()


def test_previous_version_runs_on_new_db(tmp_path):
    """前の版 (67b0e63) のコードが、新しい版で使った DB で一覧・返信・受信でき、
    前の版が入れたメールにも DB がドメインを入れる (会社名は新しい版に戻ったときに読む)."""
    old_dir = _old_code_dir(tmp_path)
    from server import clients, db, mail
    data = tmp_path / "data"
    data.mkdir()
    conn = db.connect(data / "zatsumu.db")
    admin = db.create_user(conn, "黒須", is_admin=True)
    db.set_setting(conn, "mail_from", OWN)
    db.set_setting(conn, "smtp_host", "smtp.example.com")
    db.set_setting(conn, "mail_save_sent", 0)
    conn.commit()
    conn.close()
    _mail(data, "tanaka@abc.co.jp", "田中", day=1)
    with db.get_db(data / "zatsumu.db") as conn:
        clients.create_client(conn, clients.Grouping(conn), "ABC", "", ["@abc.co.jp"])
    out = _run_old(old_dir, data, f"""
        from email.message import EmailMessage
        from fastapi.testclient import TestClient
        from server import app as app_module, mail, notify, db
        sent = []
        notify.smtp_send = lambda s, sender, rcpts, raw: sent.append(list(rcpts)) or {{}}
        c = TestClient(app_module.app)
        h = {{"Authorization": "Bearer {admin['token']}"}}
        rows = c.get("/api/mail", headers=h).json()
        d = c.get("/api/mail/1", headers=h).json()
        r = c.post("/api/mail/1/send", headers=h, json={{"body": "前の版の返信"}})
        class FakeIMAP:
            def select(self, folder, readonly=False): return ("OK", [b"1"])
            def response(self, key): return ("UIDVALIDITY", [b"1"])
            def uid(self, cmd, *args):
                if cmd == "search": return ("OK", [b"1"])
                m = EmailMessage()
                m["From"] = "WordPress <{WP}>"
                m["To"] = "{OWN}"
                m["Subject"] = "前の版で受信"
                m["Message-ID"] = "<oldfetch@x>"
                m.set_content({FORM_BODY!r})
                return ("OK", [(b"h", m.as_bytes())])
            def logout(self): pass
        mail._imap_connect = lambda s: FakeIMAP()
        with db.get_db({str(data / "zatsumu.db")!r}) as conn:
            s = {{**db.get_settings(conn), "imap_host": "imap.example.com", "mail_auto_draft": 0}}
            added = mail.fetch_new_mail(conn, s)
        assert app_module.__file__.startswith({str(old_dir)!r}), app_module.__file__
        assert "contact_domain" not in open(mail.__file__, encoding="utf-8").read()
        print(json.dumps({{"count": len(rows), "subject": d["subject"], "send": r.status_code,
                          "rcpts": sent, "added": len(added)}}))
    """)
    assert out["subject"].startswith("件名")
    assert (out["count"], out["send"], out["rcpts"], out["added"]) == \
        (1, 200, [["tanaka@abc.co.jp"]], 1)
    conn = db.connect(data / "zatsumu.db")
    try:
        row = conn.execute("SELECT * FROM mails WHERE message_id = '<oldfetch@x>'").fetchone()
        # 前の版が決めた相手 (フォームのお客様) のドメインを、DB のトリガーが入れている
        assert (row["contact_addr"], row["contact_domain"], row["contact_company"]) == \
            ("hanako@yamada-fudosan.co.jp", "yamada-fudosan.co.jp", "")
        assert mail.fill_missing_companies(conn) == 1
        items = {c["key"]: c for c in clients.list_clients(conn)["clients"]}
        assert items["d:yamada-fudosan.co.jp"]["name"] == "株式会社やまだ不動産"
        assert items["c:1"]["replies"] == 1 and items["c:1"]["name"] == "ABC"
    finally:
        conn.close()


# ---------- 画面 ----------

def test_mail_page_has_client_screens(client):
    page = client.get("/mail").text
    for marker in ('id="nav-clients">取引先</button>', 'id="clients-view"', 'id="client-view"',
                   'id="cl-q"', 'id="cd-timeline"', 'id="cd-admin"', 'id="m-client-link"',
                   "この取引先のやり取りをすべて見る", "このメールを開く", "📩 届いたメール", "↩ こちらの返信",
                   'id="set-client-personal"', "mail_client_personal_domains",
                   "別の取引先に分ける", "ほかの取引先にまとめる", "この取引先を消す",
                   'window.addEventListener("popstate"', "history.pushState"):
        assert marker in page, marker
    # 受信箱の上の長い説明は「使い方を見る」で開け閉めし、開いたかはブラウザに覚えておく
    assert '<details class="howto" data-howto="inbox">' in page
    assert '<details class="howto" data-howto="clients">' in page
    assert '<details class="howto" data-howto="client">' in page
    assert 'id="staff-help-box" data-howto="staff"' in page
    assert "savePref(HOWTO_KEY + d.dataset.howto" in page
    inbox = page[page.index('<section id="inbox"'):page.index('id="cat-tabs"')]
    assert "<p><small>受信メールを新しい順" not in inbox.replace(
        '<summary>使い方を見る</summary>\n  <p><small>受信メールを新しい順', "")
    # 外から来た文字 (名前・アドレス・件名・本文) は esc() か textContent で入れる
    for safe in ("${esc(c.name)", "${esc(sub)", "${esc(t.subject)", "${esc(t.snippet)",
                 "${esc(who)}", "${esc(a.addr)}", "${hl(m.client_name, terms)}", "${esc(ruleText(r.pattern))}",
                 '$("cd-name").textContent', '$("cd-note-view").textContent',
                 '$("m-client-name").textContent', "el.textContent = tlBodies.get(cacheKey)"):
        assert safe in page, safe
    for unsafe in ("${c.name}", "${t.subject}", "${t.snippet}", "${a.addr}", "${m.client_name}",
                   "${d.note}"):
        assert unsafe not in page, unsafe
    # 変えるボタンは管理者だけに出す (サーバも確かめる)
    assert '$("cd-admin").hidden = !admin;' in page
    assert "admin && d.address_count > 1" in page
    # 新着の自動確認は、取引先の画面を描き直さない (受信箱を見ているときだけ取り直す)
    assert 'if (viewName === "inbox" && $("mail-modal").hidden) showInbox({ auto: true });' in page
    # 返信画面で送った・変えた後も、取引先の画面から受信箱へ勝手に切り替えない
    assert page.count("inboxChanged();") >= 8
    send = page[page.index('$("m-send").onclick'):page.index('$("m-archive").onclick')]
    assert "showInbox()" not in send
    # 別の取引先を開いたら、前の取引先の設定欄の知らせ・書きかけのアドレスを消す
    show = page[page.index("async function showClient("):page.index("async function loadClient(")]
    assert 'cdMsg("");' in show and '$("cd-rule-in").value = "";' in show
    # 消した後は前の画面に戻る (同じ一覧が履歴に2つ並ばない)
    delete = page[page.index('$("cd-delete").onclick'):page.index('$("cd-name-create").onclick')]
    assert "history.back()" in delete
    # スマホ幅の取引先の一覧は、右側の数を左寄せの横並びにする
    mobile = page[page.index("@media (max-width: 720px)"):page.index("@media (max-width: 640px)")]
    assert ".cl-side { flex-direction: row;" in mobile
