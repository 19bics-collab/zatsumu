"""相手 (お客様) を決める のテスト (HPのお問い合わせフォームから届くメール).

HPのお問い合わせフォーム (WordPress の Contact Form 7 など) から届くメールは、どのお客様の
分でも差出人が同じ (wordpress@…) で、お客様は Reply-To か本文の「メールアドレス: …」にいる。

- Reply-To を読む (最初の正しいアドレスと名前)
- 本文の書き方 (全角のコロン・【】・名前とアドレスが1行・次の行に値) からお客様を読む
- 自社のドメイン・設定「フォームの送信元」・Gmail などのドメインの扱い
- アドレスが分からないフォームのメールは、ほかとまとめない
- 前からあるメールの相手を起動時に決める (2回目は何もしない・何万通でも速い)
- やり取り・「やり取り N件」・お知らせ・週ごとのまとめ・Notion・カレンダー・定型文が相手を使う
- 返信の宛先 (既定・直したとき・確かめ・記録)、確認のお願いでも同じ宛先
- 前にフォームの送信元へ送ってしまった返信の注意と、設定画面の数
- 前の版に戻っても (前の版のコードが新しい DB で) 一覧・返信ができる

ネットワークには出ない (IMAP・SMTP は偽物に差し替える)。
"""
import email
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth
from tests.test_mail_attachments import FakeIMAP

ROOT = Path(__file__).resolve().parent.parent
OWN = "info@bics.co.jp"
WP = "wordpress@bics.co.jp"

# Contact Form 7 の既定の本文 (日本語の WordPress)
CF7_DEFAULT = """差出人: 山田 花子 <hanako.yamada@example.net>
題名: 民泊の運営代行について

メッセージ本文:
都内で2部屋の民泊を始めたいと考えています。
料金を教えてください。

--
このメールは ビックス (https://bics.co.jp) のお問い合わせフォームから送信されました"""

# 項目ごとの行 (全角のコロン・会社名とお名前・全角のアットマーク)
CF7_FIELDS = """ホームページからお問い合わせがありました。

■会社名：株式会社やまだ不動産
■お名前（フリガナ）：ヤマダ ハナコ
■お名前（漢字）※必須：山田 花子 様
■メールアドレス：hanako.yamada＠example.net
■メールアドレス（確認用）：hanako.yamada@example.net
■電話番号：03-1234-5678

■お問い合わせ内容：
運営代行の料金を教えてください。
お返事は info@bics.co.jp ではなく、上のアドレスにお願いします。"""

# 【】の項目で、値が次の行
CF7_BRACKETS = """【お名前】
小林 健
【メールアドレス】
ken.kobayashi@example.org
【お問い合わせ内容】
来月から週2回の清掃をお願いしたいです。"""

# お客様のアドレスが書かれていない (電話番号だけ)
CF7_NO_ADDRESS = """お名前：中村 様
電話番号：090-0000-1111

メッセージ本文:
メールより電話で相談したいです。"""


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
    for key in ("ZATSUMU_DOMAIN", "ZATSUMU_MAIL_DOMAIN", "ZATSUMU_CADDYFILE"):
        monkeypatch.delenv(key, raising=False)
    import importlib
    from server import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture()
def users(client, tmp_path):
    """メール担当者・管理者 (差出人は info@bics.co.jp)."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        staff = db.create_user(conn, "田中")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        admin = db.create_user(conn, "黒須", is_admin=True)
        db.set_setting(conn, "mail_from", OWN)
        db.set_setting(conn, "smtp_host", "smtp.example.com")
        db.set_setting(conn, "mail_save_sent", 0)
    return staff, admin


@pytest.fixture()
def sent(monkeypatch):
    """送った返信 (notify.smtp_send) を送らずに覚える ({"to": [...], "msg": ...})."""
    from server import notify
    out = []

    def fake(settings, sender, rcpts, message):
        out.append({"rcpts": list(rcpts), "msg": email.message_from_string(message)})
        return {}
    monkeypatch.setattr(notify, "smtp_send", fake)
    monkeypatch.setattr(notify, "deliver_async", lambda *a, **kw: None)
    return out


def _db(tmp_path):
    from server import db
    return db.get_db(tmp_path / "zatsumu.db")


def _row(tmp_path, mid):
    with _db(tmp_path) as conn:
        return dict(conn.execute("SELECT * FROM mails WHERE id = ?", (mid,)).fetchone())


def _rules(**settings):
    from server import mail
    return mail.ContactRules({"mail_from": OWN, "imap_user": OWN, **settings})


def _raw(msgid, frm, subject="お問い合わせ", body="本文です", reply_to=None, to=OWN,
         in_reply_to="", references=""):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if reply_to:
        m["Reply-To"] = reply_to
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["Date"] = "Tue, 11 Aug 2026 10:00:00 +0900"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content(body)
    return m.as_bytes()


def _fetch(tmp_path, monkeypatch, mails):
    from server import db, mail
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP(mails))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com", "mail_auto_draft": 0}
        return mail.fetch_new_mail(conn, s)


def _id_of(tmp_path, msgid):
    with _db(tmp_path) as conn:
        return conn.execute("SELECT id FROM mails WHERE message_id = ?", (msgid,)).fetchone()[0]


def _form_mail(tmp_path, n, body, from_addr=WP, **kw):
    """フォームから届いたメールを、届いたときと同じように相手を決めて入れる."""
    from server import mail
    mid = _add_mail(tmp_path, n=n, from_addr=from_addr, from_name="WordPress", body=body, **kw)
    with _db(tmp_path) as conn:
        r = dict(conn.execute("SELECT * FROM mails WHERE id = ?", (mid,)).fetchone())
        c = mail.decide_contact(r, mail.contact_rules(conn))
        conn.execute("UPDATE mails SET contact_addr = ?, contact_name = ?, contact_source = ?,"
                     " contact_via_form = ? WHERE id = ?",
                     (c["contact_addr"], c["contact_name"], c["contact_source"],
                      c["contact_via_form"], mid))
    return mid


def _detail(client, user, mid):
    r = client.get(f"/api/mail/{mid}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


def _list(client, user):
    return {m["id"]: m for m in client.get("/api/mail", headers=auth(user)).json()}


# ---------- Reply-To を読む ----------

def test_parse_message_reads_first_valid_reply_to():
    from server import mail
    p = mail.parse_message(_raw("<a@x>", f"WordPress <{WP}>",
                                reply_to='"山田 花子" <hanako@example.net>'))
    assert (p["reply_to_addr"], p["reply_to_name"]) == ("hanako@example.net", "山田 花子")
    # 読めないもの・送れない形は飛ばして、最初の正しいもの
    raw = (f"From: {WP}\r\nTo: {OWN}\r\nSubject: x\r\nMessage-ID: <b@x>\r\n"
           "Reply-To: undisclosed-recipients:;\r\n"
           "Reply-To: not-an-address, ken@example.org, other@example.org\r\n"
           "\r\n本文\r\n").encode()
    p = mail.parse_message(raw)
    assert (p["reply_to_addr"], p["reply_to_name"]) == ("ken@example.org", "")
    p = mail.parse_message(_raw("<c@x>", WP))
    assert (p["reply_to_addr"], p["reply_to_name"]) == ("", "")


# ---------- 本文からお客様を読む ----------

@pytest.mark.parametrize("body, addr, name", [
    (CF7_DEFAULT, "hanako.yamada@example.net", "山田 花子"),
    (CF7_FIELDS, "hanako.yamada@example.net", "株式会社やまだ不動産 山田 花子"),
    (CF7_BRACKETS, "ken.kobayashi@example.org", "小林 健"),
    (CF7_NO_ADDRESS, "", "中村"),
    ("[ご担当者名] 鈴木\n[貴社名] スズキ商事\n[E-mail] suzuki@suzuki.example.jp\n",
     "suzuki@suzuki.example.jp", "スズキ商事 鈴木"),
    ("氏名 : 佐藤一郎\nEmail : ichiro.sato@example.com\n", "ichiro.sato@example.com", "佐藤一郎"),
    ("From: Taro Yamada <taro@example.com>\nSubject: hi\n\nMessage Body:\nhello",
     "taro@example.com", "Taro Yamada"),
    # 項目の行が無ければ、本文の最初のお客様らしいアドレス (自分たち・noreply は使わない)
    ("返事は noreply@bics.co.jp ではなく info@bics.co.jp か cust@example.jp へ", "cust@example.jp", ""),
])
def test_contact_from_contact_form_bodies(body, addr, name):
    from server import mail
    assert mail.contact_from_body(body, _rules(), WP, True) == (addr, name)


def test_decide_contact_form_body_and_reply_to():
    from server import mail
    rules = _rules()
    base = {"from_addr": WP, "from_name": "WordPress"}
    c = mail.decide_contact({**base, "body": CF7_FIELDS}, rules)
    assert c == {"contact_addr": "hanako.yamada@example.net",
                 "contact_name": "株式会社やまだ不動産 山田 花子",
                 "contact_source": "body", "contact_via_form": 1}
    # Reply-To があればそれを使う (名前が無ければ本文から)
    c = mail.decide_contact({**base, "body": CF7_FIELDS, "reply_to_addr": "h@example.net",
                             "reply_to_name": ""}, rules)
    assert (c["contact_addr"], c["contact_name"], c["contact_source"], c["contact_via_form"]) == \
        ("h@example.net", "株式会社やまだ不動産 山田 花子", "reply_to", 1)
    # 自分たちのアドレスの Reply-To・差出人と同じ Reply-To は使わない
    c = mail.decide_contact({**base, "body": CF7_BRACKETS, "reply_to_addr": OWN}, rules)
    assert (c["contact_addr"], c["contact_source"]) == ("ken.kobayashi@example.org", "body")
    # ふつうの人の Reply-To (返事は同じ会社の別のアドレスに) は、名前は差出人のまま。フォームではない
    c = mail.decide_contact({"from_addr": "taro@example.com", "from_name": "太郎", "body": "",
                             "reply_to_addr": "taro.sales@example.com", "reply_to_name": ""}, rules)
    assert c == {"contact_addr": "taro.sales@example.com", "contact_name": "太郎",
                 "contact_source": "reply_to", "contact_via_form": 0}
    # ふつうの人のメールで、Reply-To が別のドメイン (メーリングリストなど) なら差出人のまま
    c = mail.decide_contact({"from_addr": "taro@example.com", "from_name": "太郎", "body": "",
                             "reply_to_addr": "taro.home@example.org", "reply_to_name": ""}, rules)
    assert (c["contact_addr"], c["contact_source"], c["contact_via_form"]) == \
        ("taro@example.com", "from", 0)
    c = mail.decide_contact({"from_addr": "taro@example.com", "from_name": "太郎",
                             "body": "メールアドレス: other@example.org",
                             "reply_to_addr": "TARO@example.com"}, rules)
    assert (c["contact_addr"], c["contact_source"]) == ("taro@example.com", "from")


def test_no_address_form_mail_has_empty_contact():
    from server import mail
    c = mail.decide_contact({"from_addr": WP, "from_name": "WordPress",
                             "body": CF7_NO_ADDRESS}, _rules())
    assert c == {"contact_addr": "", "contact_name": "中村", "contact_source": "body",
                 "contact_via_form": 1}


def test_form_sender_kinds_and_own_domain():
    from server import mail
    rules = mail.ContactRules({"mail_from": OWN, "imap_user": OWN},
                              [{"from_addr": "kurosu@bics.co.jp"}], people=["tanaka@bics.co.jp"])
    kinds = {a: rules.form_kind(a) for a in (
        WP, "WordPress@other.example", "no-reply@airbnb.example", "donotreply@x.example",
        "form@x.example", "info-form@x.example", "contact@x.example", "taro@x.example",
        "web@bics.co.jp", "www.form@shop.example", OWN, "kurosu@bics.co.jp",
        "tanaka@bics.co.jp", "taro@sub.bics.co.jp")}
    assert kinds == {
        WP: "form", "WordPress@other.example": "form", "no-reply@airbnb.example": "maybe",
        "donotreply@x.example": "maybe", "form@x.example": "form",
        "info-form@x.example": "form", "contact@x.example": "maybe", "taro@x.example": "",
        # 自社のドメインで、受信するアドレスでも登録されている人でもない
        "web@bics.co.jp": "own_domain", "taro@sub.bics.co.jp": "own_domain",
        "www.form@shop.example": "form", OWN: "", "kurosu@bics.co.jp": "",
        "tanaka@bics.co.jp": ""}
    # 「かもしれない」送信元は、本文に「メールアドレス」の行があるときだけフォームにする
    c = mail.decide_contact({"from_addr": "web@bics.co.jp", "from_name": "HP",
                             "body": "お名前: 山田\nメールアドレス: y@example.net"}, rules)
    assert (c["contact_addr"], c["contact_name"], c["contact_source"]) == \
        ("y@example.net", "山田", "body")
    # 社内の人が転送したメール (「差出人:」の行だけ) は、その人のまま
    c = mail.decide_contact({"from_addr": "web@bics.co.jp", "from_name": "HP",
                             "body": "転送します\n差出人: A <a@example.net>"}, rules)
    assert (c["contact_addr"], c["contact_source"]) == ("web@bics.co.jp", "from")
    # Airbnb などのお知らせ (no-reply@) は、項目の行が無ければ差出人のまま (返信もしない)
    c = mail.decide_contact({"from_addr": "no-reply@airbnb.example", "from_name": "Airbnb",
                             "body": "予約が入りました。support@airbnb.example"}, rules)
    assert (c["contact_addr"], c["contact_source"]) == ("no-reply@airbnb.example", "from")


def test_free_mail_domain_is_not_our_form():
    """Gmail で受信していても、Gmail の人を「自社のフォーム」とはみなさない."""
    from server import mail
    rules = mail.ContactRules({"mail_from": "bics.office@gmail.com"})
    assert rules.form_kind("customer@gmail.com") == ""
    c = mail.decide_contact({"from_addr": "customer@gmail.com", "from_name": "お客様",
                             "body": "メールアドレス: other@example.net"}, rules)
    assert (c["contact_addr"], c["contact_source"]) == ("customer@gmail.com", "from")


def test_form_senders_setting():
    from server import mail
    assert mail.clean_form_senders(
        " Form@Shop.Example \n@forms.example.jp、 example.org\nForm@shop.example\n") == \
        "form@shop.example\n@forms.example.jp\n@example.org"
    assert mail.clean_form_senders("") == ""
    for bad in ("not an address", "a@b", "@", "x@-bad-.example"):
        with pytest.raises(ValueError, match="正しくありません"):
            mail.clean_form_senders(bad)
    with pytest.raises(ValueError, match="文字以内"):
        mail.clean_form_senders("a@example.com\n" * 300)
    rules = _rules(mail_form_senders="sales-site@shop.example\n@forms.example.jp")
    assert rules.form_kind("sales-site@shop.example") == "form"
    assert rules.form_kind("anyone@forms.example.jp") == "form"
    c = mail.decide_contact({"from_addr": "sales-site@shop.example", "from_name": "ショップ",
                             "body": "ご注文者: 山田\n連絡先：yamada@example.net"}, rules)
    assert (c["contact_addr"], c["contact_source"], c["contact_via_form"]) == \
        ("yamada@example.net", "body", 1)


def test_settings_api_form_senders_and_redecide(client, users, tmp_path):
    """設定で送信元を足すと届いていたメールも直り、外すと差出人に戻る."""
    _, admin = users
    mid = _add_mail(tmp_path, n=1, from_addr="site@shop.example", from_name="ショップ",
                    body="お名前：山田\n連絡先：yamada@example.net")
    other = _add_mail(tmp_path, n=2, from_addr="taro@example.com", body="連絡先：x@example.net")
    with _db(tmp_path) as conn:
        from server import mail
        mail.fill_missing_contacts(conn)
    assert _row(tmp_path, mid)["contact_addr"] == "site@shop.example"
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_form_senders": "Site@Shop.example"})
    assert r.status_code == 200, r.text
    assert r.json()["mail_form_senders"] == "site@shop.example"
    row = _row(tmp_path, mid)
    assert (row["contact_addr"], row["contact_name"], row["contact_source"],
            row["contact_via_form"]) == ("yamada@example.net", "山田", "body", 1)
    assert _row(tmp_path, other)["contact_addr"] == "taro@example.com"   # 関係ないメールはそのまま
    assert _list(client, admin)[mid]["via_form"] is True
    r = client.patch("/api/settings", headers=auth(admin), json={"mail_form_senders": ""})
    assert r.status_code == 200
    row = _row(tmp_path, mid)
    assert (row["contact_addr"], row["contact_source"], row["contact_via_form"]) == \
        ("site@shop.example", "from", 0)
    r = client.patch("/api/settings", headers=auth(admin), json={"mail_form_senders": "おかしな行"})
    assert r.status_code == 400 and "正しくありません" in r.json()["detail"]


# ---------- 受信したとき ----------

def test_fetch_splits_form_customers_and_keeps_same_customer_together(
        client, users, tmp_path, monkeypatch):
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<f1@bics>", f"WordPress <{WP}>", "【HP】運営代行", CF7_DEFAULT),
        2: _raw("<f2@bics>", f"WordPress <{WP}>", "【HP】清掃", CF7_BRACKETS),
        3: _raw("<f3@bics>", f"WordPress <{WP}>", "【HP】電話希望", CF7_NO_ADDRESS),
        4: _raw("<f4@bics>", f"WordPress <{WP}>", "【HP】追加の質問", CF7_FIELDS),
        5: _raw("<f5@bics>", f"WordPress <{WP}>", "【HP】もう一件", CF7_NO_ADDRESS),
        # 同じお客様が、あとから自分のメールソフトで送ってきた
        6: _raw("<d1@example.net>", "山田 花子 <Hanako.Yamada@example.net>", "Re: 料金", "続きです"),
        # Reply-To にお客様が入っているフォーム
        7: _raw("<f7@bics>", f"WordPress <{WP}>", "【HP】Reply-To", "本文だけ",
                reply_to="佐藤 <sato@example.jp>"),
    })
    ids = {n: _id_of(tmp_path, m) for n, m in (
        (1, "<f1@bics>"), (2, "<f2@bics>"), (3, "<f3@bics>"), (4, "<f4@bics>"),
        (5, "<f5@bics>"), (6, "<d1@example.net>"), (7, "<f7@bics>"))}
    rows = _list(client, admin)
    # 山田さん (フォーム2通 + 自分のメール1通) はまとまり、ほかのお客様とは混ざらない
    assert [rows[ids[n]]["thread_count"] for n in (1, 4, 6)] == [2, 2, 2]
    assert rows[ids[2]]["thread_count"] == 0 and rows[ids[7]]["thread_count"] == 0
    # アドレスが分からないフォームのメールどうしは、まとめない
    assert rows[ids[3]]["thread_count"] == rows[ids[5]]["thread_count"] == 0
    assert _detail(client, admin, ids[3])["thread"] == []
    assert sorted(t["id"] for t in _detail(client, admin, ids[1])["thread"]) == \
        sorted([ids[4], ids[6]])
    assert ids[2] not in [t["id"] for t in _detail(client, admin, ids[4])["thread"]]
    # 受信箱には、フォームではなくお客様が出る
    r1, r2, r3, r7 = (rows[ids[n]] for n in (1, 2, 3, 7))
    assert (r1["contact_name"], r1["contact_addr"], r1["via_form"]) == \
        ("山田 花子", "hanako.yamada@example.net", True)
    assert (r1["from_addr"], r1["contact_source"]) == (WP, "body")
    assert (r2["contact_name"], r2["contact_addr"]) == ("小林 健", "ken.kobayashi@example.org")
    assert (r3["contact_name"], r3["contact_addr"], r3["via_form"]) == ("中村", "", True)
    assert (r7["contact_name"], r7["contact_addr"], r7["contact_source"], r7["via_form"]) == \
        ("佐藤", "sato@example.jp", "reply_to", True)
    assert rows[ids[6]]["via_form"] is False
    assert _row(tmp_path, ids[7])["reply_to"] == "sato@example.jp"


def test_form_sent_from_our_own_address_with_reply_to_is_kept(client, users, tmp_path, monkeypatch):
    """差出人を自分たちのアドレスにしたフォーム (Reply-To がお客様) は、控えと思って捨てない."""
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<own1@bics>", f"HP <{OWN}>", "【HP】お問い合わせ", "お名前：森\n内容：見積もり",
                reply_to="mori@example.net"),
        2: _raw("<own2@bics>", f"黒須 <{OWN}>", "Re: 送った返信の控え", "控え"),
    })
    with _db(tmp_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM mails")]
    assert [(r["message_id"], r["contact_addr"], r["contact_name"], r["contact_via_form"])
            for r in rows] == [("<own1@bics>", "mori@example.net", "森", 1)]


def test_classify_and_templates_use_customer_name(client, users, tmp_path):
    """定型文の {相手の名前}・定型文の下書き・VIP は、フォームの送信元ではなくお客様."""
    from server import db, mail
    _, admin = users
    mid = _form_mail(tmp_path, 1, CF7_DEFAULT)
    tpls = client.get(f"/api/mail/{mid}/templates", headers=auth(admin)).json()
    assert tpls and all("WordPress" not in t["body"] for t in tpls)
    assert any("山田 花子 様" in t["body"] for t in tpls)
    row = _row(tmp_path, mid)
    assert mail.template_reply({}, row).startswith("山田 花子 様")
    assert mail.template_reply({}, _row(tmp_path, _form_mail(tmp_path, 2, "本文のみ"))) \
        .startswith("ご担当者 様")
    with _db(tmp_path) as conn:
        s = {**db.get_settings(conn), "mail_vip_addresses": "hanako.yamada@example.net"}
    assert mail.rule_priority(s, row) == (1, "VIP差出人")
    prompt = mail._mail_for_prompt(row)
    assert "お客様: 山田 花子 <hanako.yamada@example.net>" in prompt and WP in prompt


# ---------- 前からあるメール (起動時に決める) ----------

def test_fill_missing_contacts_is_idempotent(tmp_path):
    from server import db, mail
    a = _add_mail(tmp_path, n=1, from_addr=WP, from_name="WordPress", body=CF7_DEFAULT)
    b = _add_mail(tmp_path, n=2, from_addr="taro@example.com", from_name="太郎")
    c = _add_mail(tmp_path, n=3, from_addr=WP, from_name="WordPress", body=CF7_NO_ADDRESS)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_from", OWN)
        # 入れたばかりのメールは、差出人を相手にしてある (前の版が入れたときと同じ)
        assert tuple(conn.execute("SELECT contact_addr, contact_source FROM mails WHERE id = ?",
                                  (a,)).fetchone()) == (WP, "")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fill_missing_contacts(conn) == 3
        assert not conn.in_transaction
        assert mail.fill_missing_contacts(conn) == 0          # 2回目は何もしない
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM mails")}
    assert (rows[a]["contact_addr"], rows[a]["contact_source"]) == \
        ("hanako.yamada@example.net", "body")
    assert (rows[b]["contact_addr"], rows[b]["contact_source"]) == ("taro@example.com", "from")
    assert (rows[c]["contact_addr"], rows[c]["contact_source"]) == ("", "body")
    # 前の版に戻っていた間に入ったメールは、次の起動で決める (決めたメールは変えない)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET contact_name = '直した名前' WHERE id = ?", (b,))
    d = _add_mail(tmp_path, n=4, from_addr=WP, from_name="WordPress", body=CF7_BRACKETS)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fill_missing_contacts(conn) == 1
        assert conn.execute("SELECT contact_addr FROM mails WHERE id = ?", (d,)).fetchone()[0] \
            == "ken.kobayashi@example.org"
        assert conn.execute("SELECT contact_name FROM mails WHERE id = ?", (b,)).fetchone()[0] \
            == "直した名前"


def test_startup_fills_contacts(client, users, tmp_path):
    from server import app as app_module
    _, admin = users
    a = _add_mail(tmp_path, n=1, from_addr=WP, body=CF7_DEFAULT)
    b = _add_mail(tmp_path, n=2, from_addr=WP, body=CF7_BRACKETS)
    assert _list(client, admin)[a]["thread_count"] == 1     # 決める前は差出人でまとまる
    with TestClient(app_module.app) as c:
        rows = {m["id"]: m for m in c.get("/api/mail", headers=auth(admin)).json()}
    assert rows[a]["thread_count"] == rows[b]["thread_count"] == 0
    assert rows[a]["contact_addr"] == "hanako.yamada@example.net"


def test_fill_missing_contacts_is_fast_for_many_mails(tmp_path):
    """何万通あっても起動が遅くならない (本文はフォームの分だけ読む・2回目は読まない)."""
    from server import db, mail
    n = 30000
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_from", OWN)
        conn.executemany(
            "INSERT INTO mails (message_id, from_addr, from_name, subject, body, received_at,"
            " fetched_at) VALUES (?, ?, ?, '件名', ?, '2026-08-01T00:00:00+00:00',"
            " '2026-08-01T00:00:00+00:00')",
            [(f"<m{i}@x>", WP if i % 10 == 0 else f"guest{i % 997}@example.com",
              "WordPress" if i % 10 == 0 else "お客様",
              (f"お名前：客{i}\nメールアドレス：c{i}@example.net\n" if i % 10 == 0 else "")
              + "本文" * 200) for i in range(n)])
    conn = db.connect(tmp_path / "zatsumu.db")
    try:
        started = time.monotonic()
        assert mail.fill_missing_contacts(conn) == n
        first = time.monotonic() - started
        started = time.monotonic()
        assert mail.fill_missing_contacts(conn) == 0
        second = time.monotonic() - started
        assert conn.execute("SELECT COUNT(DISTINCT lower(contact_addr)) FROM mails"
                            " WHERE contact_source = 'body'").fetchone()[0] == n // 10
    finally:
        conn.close()
    assert first < 20, first
    assert second < 0.5, second


def test_existing_db_gets_contact_columns(tmp_path):
    """前の版の DB を開くと列・索引が足され、前からあるメールは差出人が相手になる (2回開いても同じ)."""
    from server import db, mail
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE mails (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
        imap_uid INTEGER, from_addr TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '',
        to_addr TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 2, priority_reason TEXT NOT NULL DEFAULT '',
        priority_source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unhandled',
        draft_reply TEXT NOT NULL DEFAULT '', draft_source TEXT NOT NULL DEFAULT '',
        draft_generated_at TEXT, reply_subject TEXT NOT NULL DEFAULT '',
        reply_body TEXT NOT NULL DEFAULT '', replied_at TEXT,
        replied_by TEXT NOT NULL DEFAULT '', references_hdr TEXT NOT NULL DEFAULT '')""")
    old.execute("INSERT INTO mails (message_id, from_addr, from_name, body, received_at,"
                " fetched_at) VALUES ('<o1@x>', ?, 'WordPress', ?, '2026-01-01', '2026-01-01')",
                (WP, CF7_DEFAULT))
    old.execute("INSERT INTO mails (message_id, from_addr, from_name, received_at,"
                " fetched_at) VALUES ('<o2@x>', 'Taro@Example.com', '太郎', '2026-01-02',"
                " '2026-01-02')")
    old.commit()
    old.close()
    for _ in range(2):
        conn = db.connect(path)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(mails)")]
        for col in ("reply_to", "contact_addr", "contact_name", "contact_source",
                    "contact_via_form", "reply_sent_to", "draft_to", "misdirect_checked_at",
                    "misdirect_checked_by"):
            assert cols.count(col) == 1, col
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"idx_mails_contact_lower", "idx_mails_via_form",
                "trg_mails_contact_default"} <= names
        assert [tuple(r) for r in conn.execute(
            "SELECT contact_addr, contact_name, contact_source FROM mails ORDER BY id")] == \
            [(WP, "WordPress", ""), ("Taro@Example.com", "太郎", "")]
        assert not conn.in_transaction
        conn.close()
    conn = db.connect(path)
    db.set_setting(conn, "mail_from", OWN)
    assert mail.fill_missing_contacts(conn) == 2
    assert conn.execute("SELECT contact_addr FROM mails WHERE id = 1").fetchone()[0] == \
        "hanako.yamada@example.net"
    conn.close()


# ---------- やり取り・数・お知らせ・まとめ・Notion・カレンダー ----------

def test_replies_to_the_same_form_mail_are_not_merged(client, users, tmp_path):
    """別々のお客様が、同じフォームのメール (自動返信・お知らせ) に返事をしても混ざらない."""
    from server import mail
    _, admin = users
    z = _form_mail(tmp_path, 1, "ご予約ありがとうございました（自動返信）",
                   message_id="<auto@bics.co.jp>")
    a = _add_mail(tmp_path, n=2, from_addr="a@example.net", references_hdr="<auto@bics.co.jp>")
    b = _add_mail(tmp_path, n=3, from_addr="b@example.org", references_hdr="<auto@bics.co.jp>")
    with _db(tmp_path) as conn:
        for mid in (a, b):
            mail.save_refs(conn, mid, "<auto@bics.co.jp>", "<auto@bics.co.jp>")
    assert [t["id"] for t in _detail(client, admin, a)["thread"]] == [z]
    assert [t["id"] for t in _detail(client, admin, b)["thread"]] == [z]
    # フォームのメール自身を開いたときは、それへの返事が並ぶ
    assert sorted(t["id"] for t in _detail(client, admin, z)["thread"]) == [a, b]


def test_reply_to_our_reply_to_a_form_customer_links_back(client, users, tmp_path, sent):
    """フォームのお客様に返信 → その返信への返事 (別のアドレスから) が同じやり取りになる."""
    _, admin = users
    f = _form_mail(tmp_path, 1, CF7_DEFAULT)
    r = client.post(f"/api/mail/{f}/send", headers=auth(admin), json={"body": "承りました"})
    assert r.status_code == 200, r.text
    reply_id = sent[0]["msg"]["Message-ID"]
    from server import mail
    q = _add_mail(tmp_path, n=2, from_addr="hanako-phone@example.jp")
    with _db(tmp_path) as conn:
        mail.save_refs(conn, q, reply_id, "")
    (t,) = _detail(client, admin, q)["thread"]
    assert t["id"] == f and t["contact_addr"] == "hanako.yamada@example.net" and t["via_form"]


def test_reminders_report_notion_calendar_use_contact(tmp_path):
    from server import db, mail, mail_events, notion
    now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    got = (now - timedelta(days=2)).isoformat()
    a = _add_mail(tmp_path, n=1, from_addr=WP, from_name="WordPress", body=CF7_DEFAULT,
                  fetched_at=got, received_at=got)
    _add_mail(tmp_path, n=2, from_addr=WP, from_name="WordPress", body=CF7_FIELDS,
              fetched_at=got, received_at=got)
    _add_mail(tmp_path, n=3, from_addr=WP, from_name="WordPress", body=CF7_BRACKETS,
              fetched_at=got, received_at=got)
    _add_mail(tmp_path, n=4, from_addr=WP, from_name="WordPress", body=CF7_NO_ADDRESS,
              fetched_at=got, received_at=got)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_from", OWN)
        mail.fill_missing_contacts(conn)
        s = db.get_settings(conn)
        rows = mail.due_reminders(conn, s, now, "2000-01-01")
        _, text = mail.remind_message(rows, s, now)
        stats = mail.weekly_stats(conn, now - timedelta(days=7), now, now)
        row = dict(conn.execute("SELECT * FROM mails WHERE id = ?", (a,)).fetchone())
    assert "山田 花子（hanako.yamada@example.net）（HPフォーム経由）" in text
    assert "小林 健（ken.kobayashi@example.org）（HPフォーム経由）" in text
    assert "中村（HPフォーム経由）" in text and "wordpress@" not in text
    # 週ごとのまとめの「よく届いた相手」は、フォームの送信元にまとめない (山田さんが2通)
    assert [(x["addr"], x["name"], x["count"]) for x in stats["top_senders"]] == \
        [("hanako.yamada@example.net", "株式会社やまだ不動産 山田 花子", 2)]
    v = notion.mail_values(row)
    assert v["sender"] == "山田 花子 <hanako.yamada@example.net>"
    assert (v["sender_name"], v["sender_email"]) == ("山田 花子", "hanako.yamada@example.net")
    assert mail_events.base_note(row).startswith("差出人: 山田 花子 <hanako.yamada@example.net>")
    subject, msg = mail.approval_request_message(s, row, "田中")
    assert "山田 花子（hanako.yamada@example.net）" in msg


def test_old_rows_without_contact_fall_back_to_sender():
    from server import mail
    assert mail.counterpart({"from_addr": "a@x.example", "from_name": "A"}) == ("A", "a@x.example")
    assert mail.counterpart({"from_addr": "a@x.example", "from_name": "A", "contact_source": "",
                             "contact_addr": ""}) == ("A", "a@x.example")
    assert mail.counterpart({"from_addr": WP, "from_name": "WordPress", "contact_source": "body",
                             "contact_addr": "", "contact_name": "中村"}) == ("中村", "")


# ---------- 返信の宛先 ----------

def test_reply_goes_to_customer_not_form_sender(client, users, tmp_path, sent):
    _, admin = users
    mid = _form_mail(tmp_path, 1, CF7_FIELDS)
    d = _detail(client, admin, mid)
    assert d["reply_default_to"] == "hanako.yamada@example.net" and d["via_form"]
    assert WP not in d["reply_cc_candidates"]
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "承りました"})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == "hanako.yamada@example.net"
    assert sent[0]["rcpts"] == ["hanako.yamada@example.net"]
    assert sent[0]["msg"]["To"] == "hanako.yamada@example.net"
    row = _row(tmp_path, mid)
    assert row["reply_sent_to"] == "hanako.yamada@example.net"
    d = _detail(client, admin, mid)
    assert d["reply_sent_to"] == "hanako.yamada@example.net" and d["misdirected"] is False
    with _db(tmp_path) as conn:
        (detail,) = [r[0] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'mail_send'")]
    assert detail.startswith("to=hanako.yamada@example.net from=") and "to_changed" not in detail


def test_reply_to_override_is_checked_and_recorded(client, users, tmp_path, sent):
    _, admin = users
    mid = _form_mail(tmp_path, 1, CF7_FIELDS)
    for bad, word in (("a@example.com\r\nBcc: evil@example.com", "改行"),
                      ("a@example.com, b@example.com", "1つだけ"),
                      ("おかしな宛先", "正しくありません"),
                      ("", "宛先を入れてください"),
                      (WP, "フォームの送信元")):
        r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                        json={"body": "本文", "to": bad})
        assert r.status_code == 400 and word in r.json()["detail"], (bad, r.text)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "to": "山田 <ｈａｎａｋｏ@example.jp>",
                          "cc": ["hanako@example.jp", "keiri@example.jp"]})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == "hanako@example.jp"
    # 宛先と同じ CC は送らない (同じ人に2通届かない)
    assert sent[0]["rcpts"] == ["hanako@example.jp", "keiri@example.jp"]
    with _db(tmp_path) as conn:
        (detail,) = [r[0] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'mail_send'")]
    assert detail.startswith("to=hanako@example.jp to_changed_from=hanako.yamada@example.net")


def test_form_mail_without_address_needs_to(client, users, tmp_path, sent):
    _, admin = users
    mid = _form_mail(tmp_path, 1, CF7_NO_ADDRESS)
    d = _detail(client, admin, mid)
    assert d["reply_default_to"] == "" and d["contact_name"] == "中村"
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 400 and "本文にお客様のメールアドレスが見つかりませんでした" in r.json()["detail"]
    assert not sent
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "to": "nakamura@example.jp"})
    assert r.status_code == 200 and sent[0]["rcpts"] == ["nakamura@example.jp"]
    with _db(tmp_path) as conn:
        (detail,) = [r[0] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'mail_send'")]
    assert "to_changed_from=(なし)" in detail


def test_refused_customer_address_goes_back_to_unhandled(client, users, tmp_path, monkeypatch):
    from server import notify
    _, admin = users
    mid = _form_mail(tmp_path, 1, CF7_FIELDS)
    monkeypatch.setattr(notify, "smtp_send",
                        lambda *a, **kw: {"hanako.yamada@example.net": (550, b"no such user")})
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 502 and "hanako.yamada@example.net" in r.json()["detail"]
    row = _row(tmp_path, mid)
    assert (row["status"], row["reply_sent_to"]) == ("unhandled", "")


def test_reply_all_cc_skips_form_and_own_addresses():
    from server import mail
    m = {"from_addr": WP, "contact_source": "body", "contact_addr": "hanako@example.net",
         "to_addr": f"{OWN}, Hanako <hanako@example.net>, noreply@bics.co.jp, keiri@example.net",
         "cc_addr": f"{WP}, form@other.example, Boss <boss@example.net>"}
    assert mail.reply_all_cc({"mail_from": OWN}, m) == ["keiri@example.net", "boss@example.net"]


def test_approval_flow_keeps_the_chosen_to(client, users, tmp_path, sent):
    """メール担当者が直した宛先は、管理者の画面にも出て、そのまま送られる."""
    staff, admin = users
    mid = _form_mail(tmp_path, 1, CF7_FIELDS)
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                    json={"body": "山田 様", "to": "hanako@example.jp"})
    assert r.status_code == 200, r.text
    assert _row(tmp_path, mid)["draft_to"] == "hanako@example.jp"
    d = _detail(client, admin, mid)
    assert (d["draft_to"], d["reply_default_to"]) == ("hanako@example.jp", "hanako@example.jp")
    with _db(tmp_path) as conn:
        (detail,) = [r[0] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'mail_approval_request'")]
    assert "to=hanako@example.jp to_changed_from=hanako.yamada@example.net" in detail
    # 承認して送信 (宛先を送ってこない古い画面でも、メール担当者が選んだ宛先に送る)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "山田 様", "approve_request": _row(tmp_path, mid)["requested_at"]})
    assert r.status_code == 200, r.text
    assert sent[0]["rcpts"] == ["hanako@example.jp"]
    assert _row(tmp_path, mid)["reply_sent_to"] == "hanako@example.jp"
    # 宛先を直さずに頼んだときは、相手のアドレス。フォームの送信元は選べない
    mid2 = _form_mail(tmp_path, 2, CF7_BRACKETS)
    r = client.post(f"/api/mail/{mid2}/request-approval", headers=auth(staff),
                    json={"body": "小林 様", "to": WP})
    assert r.status_code == 400 and "フォームの送信元" in r.json()["detail"]
    r = client.post(f"/api/mail/{mid2}/request-approval", headers=auth(staff),
                    json={"body": "小林 様"})
    assert r.status_code == 200
    assert _row(tmp_path, mid2)["draft_to"] == "ken.kobayashi@example.org"
    # 差し戻した後も、選んだ宛先が残る (頼み直すときの宛先の欄)
    r = client.post(f"/api/mail/{mid2}/send-back", headers=auth(admin), json={"note": "直して"})
    assert r.status_code == 200
    assert _detail(client, staff, mid2)["reply_default_to"] == "ken.kobayashi@example.org"


# ---------- フォームの送り方のいろいろ (Reply-To・HTMLの表・紹介のメールなど) ----------

def _html_form(msgid, frm, name, addr, spaced=False):
    """表の形のHTMLだけのフォームのメール (formrun など)."""
    sep = "\n  " if spaced else ""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, OWN, "お問い合わせ"
    m["Message-ID"], m["Date"] = msgid, "Tue, 11 Aug 2026 10:00:00 +0900"
    m.set_content(
        f"<p>フォームに回答がありました</p><table>{sep}<tr>{sep}<th>お名前</th>{sep}"
        f"<td>{name}</td>{sep}</tr>{sep}<tr><th class=\"l\">メールアドレス</th>{sep}"
        f"<td>{addr}</td></tr><tr><th>内容</th><td>見積もり</td></tr></table>", subtype="html")
    return m.as_bytes()


@pytest.mark.parametrize("reply_to", ["webmaster@bics.co.jp", "noreply@bics.co.jp",
                                      "no-reply@forms.example", WP])
def test_form_reply_to_that_is_not_a_customer_is_ignored(
        client, users, tmp_path, monkeypatch, sent, reply_to):
    """送信のプラグインが、どのメールにも同じ Reply-To (自社の webmaster@・noreply@ など) を付けても、
    お客様ごとに分かれ、返信もお客様に届く."""
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<rt1@x>", WP, body="お名前: 山田\nメールアドレス: yamada@ex.net\n内容: A",
                reply_to=reply_to),
        2: _raw("<rt2@x>", WP, body="お名前: 佐藤\nメールアドレス: sato@ex.org\n内容: B",
                reply_to=reply_to),
    })
    a, b = _id_of(tmp_path, "<rt1@x>"), _id_of(tmp_path, "<rt2@x>")
    rows = _list(client, admin)
    assert [(rows[i]["contact_addr"], rows[i]["contact_name"], rows[i]["via_form"],
             rows[i]["thread_count"]) for i in (a, b)] == [
        ("yamada@ex.net", "山田", True, 0), ("sato@ex.org", "佐藤", True, 0)]
    assert _detail(client, admin, a)["reply_default_to"] == "yamada@ex.net"
    r = client.post(f"/api/mail/{a}/send", headers=auth(admin), json={"body": "回答です"})
    assert r.status_code == 200, r.text
    assert sent[0]["rcpts"] == ["yamada@ex.net"]


def test_own_sender_form_with_unusable_reply_to(client, users, tmp_path, monkeypatch):
    """差出人を自分たちのアドレスにしたフォームで Reply-To が webmaster@ でも、本文のお客様にする
    (見つからなければ空。自分たちを相手にしない)."""
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<os1@x>", OWN, body="お名前: 森\nメールアドレス: mori@ex.net\n内容: A",
                reply_to="webmaster@bics.co.jp"),
        2: _raw("<os2@x>", OWN, body=CF7_NO_ADDRESS, reply_to="webmaster@bics.co.jp"),
    })
    a, b = _id_of(tmp_path, "<os1@x>"), _id_of(tmp_path, "<os2@x>")
    assert [(_row(tmp_path, i)["contact_addr"], _row(tmp_path, i)["contact_via_form"])
            for i in (a, b)] == [("mori@ex.net", 1), ("", 1)]
    assert _detail(client, admin, b)["reply_default_to"] == ""


@pytest.mark.parametrize("frm", ["noreply@formrun.example", "noreply@bics.co.jp"])
@pytest.mark.parametrize("spaced", [False, True])
def test_html_table_form_is_split_by_customer(client, users, tmp_path, monkeypatch, frm, spaced):
    """表の形のHTMLだけのフォーム (区切りもコロンも無い) でも、お客様ごとに分かれる."""
    from server import mail
    _, admin = users
    _fetch(tmp_path, monkeypatch, {1: _html_form("<h1@x>", frm, "山田", "yamada@ex.net", spaced),
                                   2: _html_form("<h2@x>", frm, "佐藤", "sato@ex.org", spaced)})
    a, b = _id_of(tmp_path, "<h1@x>"), _id_of(tmp_path, "<h2@x>")
    assert "お名前\t山田" in _row(tmp_path, a)["body"]
    rows = _list(client, admin)
    assert [(rows[i]["contact_addr"], rows[i]["contact_name"], rows[i]["via_form"],
             rows[i]["thread_count"]) for i in (a, b)] == [
        ("yamada@ex.net", "山田", True, 0), ("sato@ex.org", "佐藤", True, 0)]
    assert _detail(client, admin, a)["reply_default_to"] == "yamada@ex.net"
    # 表でないHTMLはこれまでどおり
    assert mail._strip_html("<p>こんにちは<br>山田です</p>") == "こんにちは\n山田です\n"


def test_partner_referral_from_contact_address_is_not_a_form():
    """取引先の contact@ からの「下記のお客様をご紹介します」は、紹介された人を相手にしない."""
    from server import mail
    rules = _rules()
    frm = "contact@partner.example.jp"
    for body in (
            "いつもお世話になっております。\n下記のお客様をご紹介します。\nお名前: 田中様\n"
            "連絡先: tanaka@third.example.jp\nよろしくお願いします。",
            "お名前: 田中様\n連絡先: tanaka@third.example.jp",
            "いつもお世話になっております。\n下記のお客様をご紹介します。\nお名前: 田中様\n"
            "メールアドレス: tanaka@third.example.jp"):
        c = mail.decide_contact({"from_addr": frm, "from_name": "鈴木", "body": body}, rules)
        assert (c["contact_addr"], c["contact_source"], c["contact_via_form"]) == \
            (frm, "from", 0), body
    # フォームの形 (見出し1行と項目) なら、contact@ からでも本文のお客様にする
    c = mail.decide_contact({"from_addr": frm, "from_name": "HP", "body":
                             "ホームページからお問い合わせがありました。\nお名前: 田中\n"
                             "メールアドレス: tanaka@third.example.jp\n内容: A"}, rules)
    assert (c["contact_addr"], c["contact_via_form"]) == ("tanaka@third.example.jp", 1)
    # 返事を受け取らない送信元 (noreply@) は、前に文があっても「メールアドレス」の項目を使う
    c = mail.decide_contact({"from_addr": "noreply@formrun.example", "from_name": "formrun",
                             "body": "回答がありました。\nご確認ください。\nお名前: 田中\n"
                                     "メールアドレス: tanaka@third.example.jp"}, rules)
    assert (c["contact_addr"], c["contact_via_form"]) == ("tanaka@third.example.jp", 1)
    # 「連絡先」のような広い項目は、必ずフォームと分かる送信元のときだけ
    c = mail.decide_contact({"from_addr": "noreply@formrun.example", "from_name": "formrun",
                             "body": "お名前: 田中\n連絡先: tanaka@third.example.jp"}, rules)
    assert (c["contact_addr"], c["contact_source"]) == ("noreply@formrun.example", "from")


@pytest.mark.parametrize("body, addr", [
    # アドレスの項目が無いフォームで、内容の中に別の人のアドレス (紹介者など) → 空 (宛先を入れてもらう)
    ("お名前: 山田\n電話: 03-0000-0000\n内容: 以前 sato@other.example.jp さんから紹介されました", ""),
    ("お名前: 山田\nメッセージ本文:\nsato@other.example.jp さんの紹介です", ""),
    # 一覧に無い「…アドレス」の項目
    ("お名前: 山田\n内容: 以前 sato@other.example.jp さんから紹介されました\n"
     "ご返信先アドレス: yamada@ex.net", "yamada@ex.net"),
    # 一覧の項目があれば、そちらを先に使う
    ("紹介者メール: ref@other.example.jp\nメールアドレス: yamada@ex.net", "yamada@ex.net"),
    # 項目の行が無く、アドレスが2つ以上 → どれか分からないので空
    ("山田です。sato@other.example.jp さんの紹介で yamada@ex.net から送っています", ""),
    # 項目の行が無く、内容より前にアドレスが1つ
    ("山田 yamada@ex.net\n内容: よろしくお願いします", "yamada@ex.net"),
])
def test_sure_form_without_address_field_does_not_pick_other_people(body, addr):
    from server import mail
    assert mail.contact_from_body(body, _rules(), WP, True)[0] == addr


def test_mailing_list_reply_to_does_not_merge_people(client, users, tmp_path, monkeypatch):
    """メーリングリスト (どの人のメールにも同じ Reply-To) で、別々の人を1つにまとめない."""
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<ml1@x>", "Alice <alice@ex.net>", body="a", reply_to="group@googlegroups.com"),
        2: _raw("<ml2@x>", "Bob <bob@ex2.net>", body="b", reply_to="group@googlegroups.com"),
    })
    a, b = _id_of(tmp_path, "<ml1@x>"), _id_of(tmp_path, "<ml2@x>")
    rows = _list(client, admin)
    assert [(rows[i]["contact_addr"], rows[i]["via_form"], rows[i]["thread_count"])
            for i in (a, b)] == [("alice@ex.net", False, 0), ("bob@ex2.net", False, 0)]
    assert _row(tmp_path, a)["reply_to"] == "group@googlegroups.com"


def test_redecide_keeps_form_mark_of_mail_from_our_own_address(client, users, tmp_path, monkeypatch):
    """設定「フォームの送信元」に自社のドメインを足しても外しても、自社のアドレスから届いた
    フォームのメール (Reply-To がお客様) の「HPフォーム経由」の印は消えない."""
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<k1@x>", OWN, body="お名前: 山田\n内容: A", reply_to="山田 <yamada@ex.net>")})
    a = _id_of(tmp_path, "<k1@x>")
    for value in ("@bics.co.jp", ""):
        r = client.patch("/api/settings", headers=auth(admin), json={"mail_form_senders": value})
        assert r.status_code == 200, r.text
        row = _row(tmp_path, a)
        assert (row["contact_addr"], row["contact_name"], row["contact_source"],
                row["contact_via_form"]) == ("yamada@ex.net", "山田", "reply_to", 1), value


def test_redecide_uses_the_remembered_reply_to(client, users, tmp_path, monkeypatch):
    """送信元を足したとき、覚えている Reply-To のお客様にする (本文にアドレスが無くても)."""
    _, admin = users
    _fetch(tmp_path, monkeypatch, {
        1: _raw("<rr1@x>", "site@shop.example", body="お名前: 山田\n内容: A",
                reply_to="yamada@ex.net")})
    a = _id_of(tmp_path, "<rr1@x>")
    assert (_row(tmp_path, a)["contact_addr"], _row(tmp_path, a)["contact_via_form"]) == \
        ("site@shop.example", 0)
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_form_senders": "site@shop.example"})
    assert r.status_code == 200, r.text
    row = _row(tmp_path, a)
    assert (row["contact_addr"], row["contact_name"], row["contact_source"],
            row["contact_via_form"]) == ("yamada@ex.net", "山田", "reply_to", 1)


def test_approval_notice_shows_the_changed_to(client, users, tmp_path, sent, monkeypatch):
    """メール担当者が宛先を直して確認をお願いしたら、管理者へのお知らせにも出る."""
    from server import mail, notify
    staff, admin = users
    got = []
    monkeypatch.setattr(notify, "deliver_async", lambda s, message, subject=None: got.append(message))
    mid = _form_mail(tmp_path, 1, CF7_FIELDS)
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                    json={"body": "山田 様", "to": "hanako@example.jp"})
    assert r.status_code == 200, r.text
    assert "・宛先を直しています: hanako@example.jp（元の相手: hanako.yamada@example.net）" in got[0]
    # 直していなければ出ない
    mid2 = _form_mail(tmp_path, 2, CF7_BRACKETS)
    r = client.post(f"/api/mail/{mid2}/request-approval", headers=auth(staff), json={"body": "x"})
    assert r.status_code == 200, r.text
    assert "宛先を直しています" not in got[1]
    # アドレスが分からないフォームのメールで宛先を入れたとき
    _, text = mail.approval_request_message(
        {}, {"subject": "s", "contact_source": "body", "contact_addr": "", "contact_name": "中村"},
        "田中", to="naka@example.jp")
    assert "・宛先を直しています: naka@example.jp（元の相手: なし）" in text


# ---------- 前にフォームの送信元へ送ってしまった返信 ----------

def test_misdirected_reply_notice_and_settings_count(client, users, tmp_path):
    staff, admin = users
    old = _form_mail(tmp_path, 1, CF7_DEFAULT, status="replied")
    other = _form_mail(tmp_path, 2, CF7_BRACKETS, status="replied")
    fine = _form_mail(tmp_path, 3, CF7_FIELDS, status="replied")
    with _db(tmp_path) as conn:
        # 前の版は差出人 (フォーム) に送っていた (reply_sent_to は空)
        conn.execute("UPDATE mails SET replied_at = '2026-08-01T00:00:00+00:00'")
        conn.execute("UPDATE mails SET reply_sent_to = ? WHERE id = ?",
                     ("hanako.yamada@example.net", fine))
    d = _detail(client, admin, old)
    assert d["misdirected"] is True and d["reply_sent_to"] == WP
    assert _detail(client, admin, fine)["misdirected"] is False
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_form_misdirected_count"] == 2
    assert {m["id"] for m in s["mail_form_misdirected"]} == {old, other}
    # 「お客様に連絡した」で消える (メール担当者も押せる)。記録に残る
    r = client.post(f"/api/mail/{old}/misdirect-checked", headers=auth(staff))
    assert r.status_code == 200
    assert _detail(client, admin, old)["misdirected"] is False
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_form_misdirected_count"] == 1
    with _db(tmp_path) as conn:
        (detail,) = [r[0] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'mail_misdirect_checked'")]
    assert detail == f"mail={old} sent_to={WP} contact=hanako.yamada@example.net"
    # ふつうのメール・未対応のメールには出ない
    plain = _add_mail(tmp_path, n=4, status="replied")
    assert _detail(client, admin, plain)["misdirected"] is False


# ---------- 画面 ----------

def test_mail_page_has_contact_ui(client):
    page = client.get("/mail").text
    for marker in ('id="m-to"', 'id="m-to-note"', 'id="m-misdir"', 'id="m-misdir-ok"',
                   'id="set-mail-form-senders"', 'id="form-misdir"', "HPフォーム経由",
                   "HPのお問い合わせフォームから届いたメールです。本文に書かれたメールアドレス宛てに"
                   "返信します。宛先が正しいか確かめてください",
                   "この返信はフォームの送信元（${m.from_addr}）宛てに送られていました。"
                   "お客様に届いていない可能性があります。",
                   "body: JSON.stringify({ body: text, cc, bcc, to,",
                   "mail_form_senders: $(\"set-mail-form-senders\").value",
                   "/misdirect-checked",
                   # 確認をお願いした後の宛先の注意・承認のときの確認に、直した宛先を出す
                   "renderControls(modal.mail);\n    renderToNote(modal.mail);",
                   "（担当者が直した宛先。元の相手: ${c}）"):
        assert marker in page, marker
    # 外から来た文字 (お客様の名前・アドレス) は esc() か textContent で入れる
    assert "${hl(contactLabel(m), T)}" in page and 'title="HPのお問い合わせフォームから届いたメールです（差出人: ${esc(m.from_addr)}）' in page
    assert '$("m-to-note").innerHTML' not in page and '$("m-misdir-text").innerHTML' not in page
    assert '$("m-misdir-text").textContent' in page and "b.textContent = `${m.subject" in page


# ---------- 前の版に戻ったとき ----------

def test_old_style_insert_and_queries_work_on_new_db(tmp_path):
    """前の版の書き込み・読み方 (相手の列を知らない) でも、新しい DB で動く."""
    from server import db, mail
    conn = db.connect(tmp_path / "zatsumu.db")
    # 前の版の受信 (_insert_mails の INSERT。相手の列を書かない)
    conn.execute(
        "INSERT OR IGNORE INTO mails (message_id, imap_uid, from_addr,"
        " from_name, to_addr, cc_addr, subject, body, received_at, fetched_at,"
        " priority, priority_reason, priority_source, category,"
        " assignee_user_id, draft_reply, draft_source, draft_generated_at,"
        " references_hdr, account_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("<old@x>", 1, WP, "WordPress", OWN, "", "件名", CF7_DEFAULT,
         "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", 2, "", "rule", "",
         None, "", "", None, "", None))
    # 前の版の一覧・やり取り (差出人で数える)
    n = conn.execute(
        "SELECT lower(from_addr), COUNT(*) FROM mails WHERE lower(from_addr) IN (?)"
        " GROUP BY lower(from_addr)", (WP,)).fetchall()
    assert [tuple(r) for r in n] == [(WP, 1)]
    row = conn.execute("SELECT * FROM mails").fetchone()
    assert (row["contact_addr"], row["contact_source"]) == (WP, "")
    # 前の版の返信 (差出人に送り、reply_sent_to は書かない) → 新しい版では注意が出る
    conn.execute("UPDATE mails SET status = 'replied', reply_body = '本文', replied_at = ?,"
                 " replied_by = '黒須' WHERE id = ?", ("2026-08-02T00:00:00+00:00", row["id"]))
    conn.commit()
    db.set_setting(conn, "mail_from", OWN)
    assert mail.fill_missing_contacts(conn) == 1
    row = conn.execute("SELECT * FROM mails").fetchone()
    assert row["contact_addr"] == "hanako.yamada@example.net" and mail.misdirected(row)
    conn.close()


def _old_code_dir(tmp_path):
    """本番が今動いている版 (232a5dc) のコードを取り出す (無ければテストを飛ばす)."""
    rev = "232a5dc"
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", rev + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip("前の版のコミットがありません (浅い clone)")
    out = tmp_path / "old_code"
    out.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", rev, "server"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(out)], input=archive, check=True)
    return out


def test_previous_version_can_list_and_reply_on_new_db(tmp_path):
    """前の版 (232a5dc) のコードが、新しい版で使った DB で一覧・返信・受信できる."""
    from server import db, mail
    old_dir = _old_code_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    conn = db.connect(data / "zatsumu.db")
    admin = db.create_user(conn, "黒須", is_admin=True)
    db.set_setting(conn, "mail_from", OWN)
    db.set_setting(conn, "smtp_host", "smtp.example.com")
    db.set_setting(conn, "mail_save_sent", 0)
    conn.commit()
    fid = _form_mail(data, 1, CF7_DEFAULT)
    _form_mail(data, 2, CF7_BRACKETS)
    conn.close()
    script = textwrap.dedent(f"""
        import json, sys
        from email.message import EmailMessage
        sys.path.insert(0, {str(old_dir)!r})
        from fastapi.testclient import TestClient
        from server import app as app_module, mail, notify, db
        sent = []
        notify.smtp_send = lambda s, sender, rcpts, raw: sent.append(list(rcpts)) or {{}}
        c = TestClient(app_module.app)
        h = {{"Authorization": "Bearer {admin['token']}"}}
        rows = c.get("/api/mail", headers=h).json()
        d = c.get("/api/mail/{fid}", headers=h).json()
        r = c.post("/api/mail/{fid}/send", headers=h, json={{"body": "前の版の返信"}})
        # 前の版の受信 (相手の列を知らない INSERT)
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
                m.set_content("【お名前】\\n森\\n【メールアドレス】\\nmori@example.net")
                return ("OK", [(b"h", m.as_bytes())])
            def logout(self): pass
        mail._imap_connect = lambda s: FakeIMAP()
        with db.get_db({str(data / "zatsumu.db")!r}) as conn:
            s = {{**db.get_settings(conn), "imap_host": "imap.example.com", "mail_auto_draft": 0}}
            added = mail.fetch_new_mail(conn, s)
        assert app_module.__file__.startswith({str(old_dir)!r}), app_module.__file__
        assert "contact_addr" not in open(mail.__file__, encoding="utf-8").read()
        print(json.dumps({{"count": len(rows), "detail_from": d["from_addr"],
                          "send": r.status_code, "rcpts": sent, "added": len(added)}}))
    """)
    env = {**os.environ, "ZATSUMU_DATA_DIR": str(data)}
    env.pop("ZATSUMU_DEMO", None)
    env.pop("ZATSUMU_LOGIN_VERIFY_EMAIL", None)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(old_dir), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    import json
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    # 前の版は今までどおり動く (差出人に送るのは前の版のまま)
    assert out == {"count": 2, "detail_from": WP, "send": 200, "rcpts": [[WP]], "added": 1}
    # 新しい版に戻ると、前の版が受信したメールの相手を決め、前の版の返信には注意が出る
    conn = db.connect(data / "zatsumu.db")
    try:
        assert mail.fill_missing_contacts(conn) == 1
        rows = {r["message_id"]: r for r in conn.execute("SELECT * FROM mails")}
        assert rows["<oldfetch@x>"]["contact_addr"] == "mori@example.net"
        assert mail.misdirected(rows["<m1@example.com>"])
    finally:
        conn.close()
