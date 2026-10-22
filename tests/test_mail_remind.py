"""返事をしていないメールのお知らせのテスト.

- 優先度ごとの待ち時間 (高 / 中・低) と、1通につき1回だけ知らせること
- お知らせしない時間帯 (日をまたぐ「21-8」など) は止めておき、終わったらまとめて送る
- 使い始めたとき (この機能を入れた直後) に、前からたまっていた古いメールをまとめて知らせない
- 送り先 (Slack・通知先メール) が無ければ何もしない・送れなければ次の回にもう一度
- お知らせの文 (Slack の特殊な書き方の無害化・メール画面のアドレス)
- 設定の確かめ・「テスト送信」・定期受信の回で動くこと・前からある DB

ネットワークには出ない (送信は偽物に差し替える)。
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.test_api import auth

# 日本時間 12:00 (お知らせしない時間帯の外)
NOW = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
SLACK = "https://hooks.slack.test/T000/B000/secret-path"


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    from server import tz
    tz.set_tz("Asia/Tokyo")
    for key in ("ZATSUMU_DOMAIN", "ZATSUMU_MAIL_DOMAIN", "ZATSUMU_CADDYFILE"):
        monkeypatch.delenv(key, raising=False)
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
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
    return worker, admin


class Notifier:
    """notify.deliver の代わり (送った文を覚え、送れた送り先を返す)."""

    def __init__(self, result=("slack",)):
        self.calls = []
        self.result = list(result)

    def __call__(self, settings, text, subject="zatsumu 通知"):
        self.calls.append({"text": text, "subject": subject})
        return list(self.result)


def _conn(tmp_path, **settings):
    from server import db
    conn = db.connect(tmp_path / "zatsumu.db")
    for key, value in {"slack_webhook_url": SLACK, **settings}.items():
        db.set_setting(conn, key, value)
    conn.commit()
    return conn


def _mail(conn, n, hours_ago, priority=2, status="unhandled", now=NOW, **kw):
    from server import tz
    cur = conn.execute(
        "INSERT INTO mails (message_id, from_addr, from_name, subject, received_at,"
        " fetched_at, priority, status, category, assignee_user_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"<r{n}@example.com>", kw.get("from_addr", f"guest{n}@example.com"),
         kw.get("from_name", ""), kw.get("subject", f"件名{n}"),
         kw.get("received_at", "2026-01-01T00:00:00+00:00"),
         tz.utc_iso(now - timedelta(hours=hours_ago)), priority, status,
         kw.get("category", ""), kw.get("assignee_user_id")))
    conn.commit()
    return cur.lastrowid


def _reminded(conn) -> set:
    return {r[0] for r in conn.execute("SELECT id FROM mails WHERE reminded_at IS NOT NULL")}


def _remind(conn, notifier, now=NOW) -> int:
    from server import mail
    return mail.remind_unanswered(conn, notifier=notifier, now=now)


def _jst(y, mo, d, h, mi=0):
    """日本時間の日時を UTC で."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=9))).astimezone(timezone.utc)


# ---------- 優先度ごとの待ち時間 ----------

def test_thresholds_per_priority(tmp_path):
    conn = _conn(tmp_path)   # 既定: 高は2時間・中と低は24時間
    high_due = _mail(conn, 1, 3, priority=1)
    _mail(conn, 2, 1.5, priority=1)                    # 高だがまだ1時間半
    _mail(conn, 3, 23, priority=2)                     # 中だがまだ23時間
    normal_due = _mail(conn, 4, 25, priority=2)
    low_due = _mail(conn, 5, 30, priority=3)
    _mail(conn, 6, 50, priority=1, status="replied")   # 返信済み・対応不要は知らせない
    _mail(conn, 7, 50, priority=2, status="archived")
    n = Notifier()
    assert _remind(conn, n) == 3
    assert _reminded(conn) == {high_due, normal_due, low_due}
    assert len(n.calls) == 1                           # まとめて1通
    text = n.calls[0]["text"]
    assert "まだ返事をしていないメールが 3 通あります" in text
    for subject in ("件名1", "件名4", "件名5"):
        assert subject in text
    for subject in ("件名2", "件名3", "件名6", "件名7"):
        assert subject not in text
    # 優先度の高い順・届いた順に並ぶ
    assert text.index("件名1") < text.index("件名4") < text.index("件名5")
    assert "[高] 件名1" in text and "[低] 件名5" in text
    assert n.calls[0]["subject"] == "まだ返事をしていないメールが 3 通あります"
    conn.close()


def test_zero_hours_turns_off_that_priority(tmp_path):
    conn = _conn(tmp_path, mail_remind_hours_normal=0)
    high = _mail(conn, 1, 3, priority=1)
    _mail(conn, 2, 100, priority=2)
    _mail(conn, 3, 100, priority=3)
    n = Notifier()
    assert _remind(conn, n) == 1 and _reminded(conn) == {high}
    assert "[高]は2時間たっても" in n.calls[0]["text"]
    assert "[中]・[低]" not in n.calls[0]["text"]

    conn.execute("UPDATE mails SET reminded_at = NULL")
    from server import db
    db.set_setting(conn, "mail_remind_hours_high", 0)
    db.set_setting(conn, "mail_remind_hours_normal", 1)
    conn.commit()
    n = Notifier()
    assert _remind(conn, n) == 2 and _reminded(conn) == {2, 3}
    # 両方 0 なら何も知らせない
    conn.execute("UPDATE mails SET reminded_at = NULL")
    db.set_setting(conn, "mail_remind_hours_normal", 0)
    conn.commit()
    n = Notifier()
    assert _remind(conn, n) == 0 and n.calls == [] and _reminded(conn) == set()
    conn.close()


def test_priority_raised_later_uses_the_high_threshold(tmp_path):
    conn = _conn(tmp_path)
    mid = _mail(conn, 1, 3, priority=2)
    n = Notifier()
    assert _remind(conn, n) == 0
    # 返信画面で優先度を[高]に変えたら、[高]の待ち時間で知らせる
    conn.execute("UPDATE mails SET priority = 1 WHERE id = ?", (mid,))
    conn.commit()
    assert _remind(conn, n, NOW + timedelta(minutes=5)) == 1
    conn.close()


def test_disabled_does_nothing(tmp_path):
    conn = _conn(tmp_path, mail_remind_enabled=0)
    _mail(conn, 1, 30, priority=1)
    n = Notifier()
    assert _remind(conn, n) == 0 and n.calls == [] and _reminded(conn) == set()
    conn.close()


# ---------- 1通につき1回 ----------

def test_each_mail_is_reminded_only_once(tmp_path):
    conn = _conn(tmp_path)
    first = _mail(conn, 1, 3, priority=1)
    n = Notifier()
    assert _remind(conn, n) == 1
    # 次の回 (5分後・翌日) には、もう知らせない
    assert _remind(conn, n, NOW + timedelta(minutes=5)) == 0
    assert _remind(conn, n, NOW + timedelta(hours=26)) == 0
    assert len(n.calls) == 1
    # 後から待ち時間を過ぎたメールだけを、次のお知らせに並べる
    second = _mail(conn, 2, 1, priority=1)
    assert _remind(conn, n, NOW + timedelta(hours=27)) == 1
    assert len(n.calls) == 2
    assert "件名2" in n.calls[1]["text"] and "件名1" not in n.calls[1]["text"]
    assert _reminded(conn) == {first, second}
    # 未対応に戻っても (送信に失敗して戻った等)、2回目は知らせない
    conn.execute("UPDATE mails SET status = 'replied' WHERE id = ?", (first,))
    conn.execute("UPDATE mails SET status = 'unhandled' WHERE id = ?", (first,))
    conn.commit()
    assert _remind(conn, n, NOW + timedelta(hours=28)) == 0
    conn.close()


def test_send_failure_is_retried_next_time(tmp_path):
    conn = _conn(tmp_path)
    mid = _mail(conn, 1, 3, priority=1)
    broken = Notifier(result=())         # Slack もメールも送れなかった
    assert _remind(conn, broken) == 0
    assert len(broken.calls) == 1 and _reminded(conn) == set()
    ok = Notifier()
    assert _remind(conn, ok, NOW + timedelta(minutes=5)) == 1
    assert _reminded(conn) == {mid}
    # 片方だけ送れたとき (Slack は届いた) は、お知らせ済みにする
    _mail(conn, 2, 3, priority=1, now=NOW + timedelta(minutes=5))
    half = Notifier(result=("slack",))
    assert _remind(conn, half, NOW + timedelta(minutes=10)) == 1
    conn.close()


def test_many_mails_are_summarized(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    ids = [_mail(conn, i, 30 + i, priority=2) for i in range(1, 14)]
    n = Notifier()
    assert _remind(conn, n) == 13
    text = n.calls[0]["text"]
    assert text.count("\n・[") + text.startswith("・[") == mail.REMIND_LIST_MAX
    assert "ほかにも 3 通あります。" in text
    # 並べなかった分も、お知らせ済みにする (毎回同じ一覧が届かないように)
    assert _reminded(conn) == set(ids)
    conn.close()


# ---------- お知らせしない時間帯 ----------

def test_quiet_hours_across_midnight():
    from server import mail
    s = {"mail_remind_quiet": "21-8"}
    for h, mi, quiet in ((20, 59, False), (21, 0, True), (23, 30, True), (0, 0, True),
                         (3, 0, True), (7, 59, True), (8, 0, False), (12, 0, False)):
        assert mail.in_quiet_hours(s, _jst(2026, 10, 22, h, mi)) is quiet, (h, mi)
    # 日をまたがない時間帯
    s = {"mail_remind_quiet": "12-13"}
    assert mail.in_quiet_hours(s, _jst(2026, 10, 22, 12, 30))
    assert not mail.in_quiet_hours(s, _jst(2026, 10, 22, 13, 0))
    assert not mail.in_quiet_hours(s, _jst(2026, 10, 22, 11, 59))
    # 空なら止めない。読めない値 (手で DB を書き換えた等) は既定の夜中の時間帯
    assert not mail.in_quiet_hours({"mail_remind_quiet": ""}, _jst(2026, 10, 22, 3))
    assert mail.in_quiet_hours({"mail_remind_quiet": "壊れた値"}, _jst(2026, 10, 22, 3))
    assert not mail.in_quiet_hours({"mail_remind_quiet": "壊れた値"}, _jst(2026, 10, 22, 12))


def test_quiet_hours_follow_the_company_timezone():
    from server import mail, tz
    s = {"mail_remind_quiet": "21-8"}
    at = datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc)   # 日本時間 22:00
    assert mail.in_quiet_hours(s, at)
    tz.set_tz("UTC")                                          # UTC なら 13:00
    assert not mail.in_quiet_hours(s, at)


def test_held_during_quiet_hours_and_sent_after(tmp_path):
    conn = _conn(tmp_path)
    night = _jst(2026, 10, 22, 23, 0)
    a = _mail(conn, 1, 3, priority=1, now=night)
    b = _mail(conn, 2, 30, priority=2, now=night)
    n = Notifier()
    # 夜中は送らず、お知らせ済みにもしない
    for at in (night, _jst(2026, 10, 23, 2, 0), _jst(2026, 10, 23, 7, 55)):
        assert _remind(conn, n, at) == 0
    assert n.calls == [] and _reminded(conn) == set()
    # 夜中の間に待ち時間を過ぎたものも一緒に、終わった後の回でまとめて送る
    c = _mail(conn, 3, 1, priority=1, now=_jst(2026, 10, 23, 1, 0))
    assert _remind(conn, n, _jst(2026, 10, 23, 8, 0)) == 3
    assert len(n.calls) == 1 and _reminded(conn) == {a, b, c}
    conn.close()


def test_quiet_hours_off(tmp_path):
    conn = _conn(tmp_path, mail_remind_quiet="")
    _mail(conn, 1, 3, priority=1, now=_jst(2026, 10, 22, 3, 0))
    assert _remind(conn, Notifier(), _jst(2026, 10, 22, 3, 0)) == 1
    conn.close()


# ---------- 使い始めたときに、古いメールをまとめて知らせない ----------

def test_no_flood_on_first_run(tmp_path):
    conn = _conn(tmp_path)
    # この機能を入れる前から未対応のまま残っていたメール
    old = _mail(conn, 1, 24 * 30, priority=1)          # 30日前に取り込んだ
    old2 = _mail(conn, 2, 24 * 8, priority=2)          # 8日前
    recent = _mail(conn, 3, 24 * 3, priority=2)        # 3日前 (直近7日なので知らせる)
    n = Notifier()
    assert _remind(conn, n) == 1
    assert _reminded(conn) == {recent}
    assert "件名1" not in n.calls[0]["text"] and "件名2" not in n.calls[0]["text"]
    # 何回動いても、古いメールは知らせない
    at = NOW
    for _ in range(4):
        at += timedelta(hours=12)
        assert _remind(conn, n, at) == 0
    assert old not in _reminded(conn) and old2 not in _reminded(conn)
    conn.close()


def test_mails_after_first_run_are_reminded_even_after_7_days(tmp_path):
    """使い始めた後に取り込んだメールは、7日より前でも (待ち時間が長い設定なら) 知らせる."""
    conn = _conn(tmp_path, mail_remind_hours_normal=168)
    n = Notifier()
    assert _remind(conn, n) == 0                       # 使い始め
    mid = _mail(conn, 1, -0.5, priority=2)             # 使い始めの30分後に取り込んだ
    at = NOW
    while at < NOW + timedelta(hours=168):             # 動き続けている (12時間ごと)
        at += timedelta(hours=12)
        assert _remind(conn, n, at) == 0
    assert _remind(conn, n, NOW + timedelta(hours=169)) == 1
    assert _reminded(conn) == {mid}
    conn.close()


def test_long_pause_starts_over(tmp_path):
    """しばらく止めていた後は、止めていた間の古いメールをまとめて知らせない (直近7日だけ)."""
    from server import db
    conn = _conn(tmp_path)
    n = Notifier()
    assert _remind(conn, n) == 0                       # 使い始め
    db.set_setting(conn, "mail_remind_enabled", 0)     # 止める
    conn.commit()
    during = _mail(conn, 1, -24, priority=2)           # 止めている間 (翌日) に取り込んだ
    later = _mail(conn, 2, -24 * 8, priority=2)        # 8日後に取り込んだ
    back = NOW + timedelta(days=10)
    assert _remind(conn, n, back) == 0                 # 止めている間は何もしない
    db.set_setting(conn, "mail_remind_enabled", 1)     # また使い始める (10日後)
    conn.commit()
    assert _remind(conn, n, back) == 1
    assert _reminded(conn) == {later}
    assert during not in _reminded(conn)
    conn.close()


# ---------- 送り先 ----------

def test_no_channels_configured(tmp_path):
    from server import db
    conn = _conn(tmp_path, slack_webhook_url="")
    mid = _mail(conn, 1, 30, priority=1)
    n = Notifier()
    assert _remind(conn, n) == 0
    assert n.calls == [] and _reminded(conn) == set()
    # 通知先メールだけ入れても、SMTP が無ければ送れないので何もしない
    db.set_setting(conn, "mail_to", "boss@example.com")
    conn.commit()
    assert _remind(conn, n) == 0 and n.calls == []
    # 送り先を入れたら、その後の回で知らせる (直近7日の分)
    db.set_setting(conn, "smtp_host", "smtp.example.com")
    conn.commit()
    assert _remind(conn, n, NOW + timedelta(minutes=5)) == 1
    assert _reminded(conn) == {mid}
    conn.close()


def test_uses_notify_deliver_by_default(tmp_path, monkeypatch):
    from server import notify
    conn = _conn(tmp_path, mail_to="boss@example.com", smtp_host="smtp.example.com")
    _mail(conn, 1, 3, priority=1)
    sent = []
    monkeypatch.setattr(notify, "send_slack", lambda url, text: sent.append(("slack", url, text)))
    monkeypatch.setattr(notify, "send_email",
                        lambda s, subject, text: sent.append(("email", subject, text)))
    from server import mail
    assert mail.remind_unanswered(conn, now=NOW) == 1
    assert [x[0] for x in sent] == ["slack", "email"]
    assert sent[0][1] == SLACK
    assert sent[1][1] == "まだ返事をしていないメールが 1 通あります"
    conn.close()


# ---------- お知らせの文 ----------

def test_message_content_is_sanitized(tmp_path):
    from server import db
    conn = _conn(tmp_path, mail_page_url="https://desk.example.com/mail")
    staff = db.create_user(conn, "佐藤")
    _mail(conn, 1, 3, priority=1, subject="至急 <!channel> 鍵が\r\n開きません‮",
          from_name="<@U123> 山田", from_addr="yamada@example.com",
          category="清掃のご依頼", assignee_user_id=staff["id"])
    _mail(conn, 2, 49, priority=3, subject="", from_addr="news@example.com")
    _mail(conn, 3, 26, priority=2, subject="あ" * 200, from_addr="")
    n = Notifier()
    assert _remind(conn, n) == 3
    text = n.calls[0]["text"]
    lines = text.split("\n")
    # 外から来た文字は1行にし、Slack の特殊な書き方にならないよう山括弧を全角にする
    assert "<" not in text and ">" not in text
    assert "＜!channel＞" in text and "＜@U123＞" in text
    assert "\r" not in text and "‮" not in text
    assert "・[高] 至急 ＜!channel＞ 鍵が 開きません — ＜@U123＞ 山田（yamada@example.com）" in lines
    assert "　　届いてから3時間／清掃のご依頼／担当: 佐藤" in lines
    assert "・[中] " + "あ" * 60 + "… — 差出人不明" in lines
    assert "　　届いてから1日2時間／未分類／担当なし" in lines
    assert "・[低] (件名なし) — news@example.com" in lines
    assert "　　届いてから2日1時間／未分類／担当なし" in lines
    assert "受信箱を開く: https://desk.example.com/mail" in lines
    assert lines[-1] == ("（優先度[高]は2時間、[中]・[低]は24時間たっても「未対応」のままの"
                         "メールを、1通につき1回お知らせしています）")
    conn.close()


def test_wait_text():
    from server import mail
    assert mail.wait_text(0) == "0分"
    assert mail.wait_text(45 * 60 + 59) == "45分"
    assert mail.wait_text(2 * 3600) == "2時間"
    assert mail.wait_text(24 * 3600) == "1日"
    assert mail.wait_text(27 * 3600 + 100) == "1日3時間"
    assert mail.wait_text(-5) == "0分"


def test_mail_page_url_from_setting_or_env(monkeypatch):
    from server import mail
    assert mail.mail_page_url({}) == ""
    # メール専用サーバ (Caddyfile.mail) は ZATSUMU_MAIL_DOMAIN で公開している
    monkeypatch.setenv("ZATSUMU_CADDYFILE", "Caddyfile.mail")
    monkeypatch.setenv("ZATSUMU_MAIL_DOMAIN", "Desk.Example.com")
    monkeypatch.setenv("ZATSUMU_DOMAIN", "localhost")
    assert mail.env_mail_page_url() == "https://desk.example.com/mail"
    # 既定の Caddyfile は ZATSUMU_DOMAIN で、勤怠と同じドメインの /mail
    monkeypatch.setenv("ZATSUMU_CADDYFILE", "")
    assert mail.env_mail_page_url() == ""             # localhost のまま (まだ公開していない)
    monkeypatch.setenv("ZATSUMU_DOMAIN", "kintai.example.com")
    assert mail.env_mail_page_url() == "https://kintai.example.com/mail"
    for bad in ("mail.localhost", "kintai", "evil.example.com/<x>", "a b.example.com", ""):
        monkeypatch.setenv("ZATSUMU_DOMAIN", bad)
        assert mail.env_mail_page_url() == "", bad
    # 設定に入れたアドレスがあれば、そちらを使う
    monkeypatch.setenv("ZATSUMU_DOMAIN", "kintai.example.com")
    assert mail.mail_page_url({"mail_page_url": "https://inbox.example.com/mail"}) == \
        "https://inbox.example.com/mail"
    assert mail.mail_page_url({"mail_page_url": "javascript:alert(1)"}) == \
        "https://kintai.example.com/mail"


# ---------- 設定 ----------

def test_remind_settings_defaults_and_validation(client, users):
    _, admin = users
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert (s["mail_remind_enabled"], s["mail_remind_hours_high"],
            s["mail_remind_hours_normal"], s["mail_remind_quiet"], s["mail_page_url"]) == \
        (1, 2, 24, "21-8", "")
    assert s["mail_page_url_auto"] == ""
    assert not [k for k in s if k.startswith("mail_state_")]

    def patch(**body):
        return client.patch("/api/settings", headers=auth(admin), json=body)

    for key, label in (("mail_remind_hours_high", "優先度[高]"),
                       ("mail_remind_hours_normal", "優先度[中]・[低]")):
        for bad in (-1, 169):
            r = patch(**{key: bad})
            assert r.status_code == 400
            assert label in r.json()["detail"] and "0〜168 の範囲" in r.json()["detail"]
        assert patch(**{key: 0}).json()[key] == 0
    # 時間帯は「21-8」の形にそろえて保存する (全角・「〜」「から」も読む)
    for raw, saved in (("２１〜８", "21-8"), ("21時から8時まで", "21-8"), ("22:00-6:00", "22-6"),
                       (" 23 - 7 ", "23-7"), ("24-6", "0-6"), ("", "")):
        r = patch(mail_remind_quiet=raw)
        assert r.status_code == 200 and r.json()["mail_remind_quiet"] == saved, raw
    for bad in ("25-8", "夜", "21", "21-8-9", "21:30-8"):
        r = patch(mail_remind_quiet=bad)
        assert r.status_code == 400 and "「21-8」のように" in r.json()["detail"], bad
    r = patch(mail_remind_quiet="8-8")
    assert r.status_code == 400 and "始まりと終わりが同じ" in r.json()["detail"]
    # メール画面のアドレス
    r = patch(mail_page_url=" https://desk.example.com/mail ")
    assert r.json()["mail_page_url"] == "https://desk.example.com/mail"
    for bad in ("desk.example.com/mail", "javascript:alert(1)", "https://a b/mail",
                "https://x/<!channel>", "https://x/a|b", "https://x/\x00a",
                "https://x/" + "a" * 300):
        r = patch(mail_page_url=bad)
        assert r.status_code == 400 and "https://" in r.json()["detail"], bad
    assert patch(mail_page_url="").json()["mail_page_url"] == ""
    # 入れた値は変わらずに残っている (エラーの保存は何も変えない)
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_remind_quiet"] == "" and s["mail_remind_hours_high"] == 0


def test_settings_show_page_url_from_env(client, users, monkeypatch):
    _, admin = users
    monkeypatch.setenv("ZATSUMU_CADDYFILE", "Caddyfile.mail")
    monkeypatch.setenv("ZATSUMU_MAIL_DOMAIN", "desk.example.com")
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_page_url_auto"] == "https://desk.example.com/mail"


def test_slack_url_is_masked_in_audit_log(client, users):
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": SLACK, "mail_to": "boss@example.com",
                       "mail_remind_quiet": "22-7"})
    csv_text = client.get("/api/reports/audit.csv?month=" +
                          datetime.now().strftime("%Y-%m"), headers=auth(admin)).text
    assert "secret-path" not in csv_text and "slack_webhook_url=***" in csv_text
    assert "mail_remind_quiet=22-7" in csv_text


# ---------- テスト送信 ----------

def test_test_remind_endpoint(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    from server import db
    worker, admin = users
    r = client.post("/api/settings/test-remind", headers=auth(admin))
    assert r.status_code == 400 and "送り先が未設定" in r.json()["detail"]
    assert client.post("/api/settings/test-remind",
                       headers=auth(worker)).status_code == 403
    client.patch("/api/settings", headers=auth(admin), json={"slack_webhook_url": SLACK})
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        _mail(conn, 1, 0.1, priority=1, subject="さっき届いた <!here>",
              now=datetime.now(timezone.utc))
    sent = []

    def deliver(s, text, subject="zatsumu 通知"):
        sent.append((text, subject))
        return ["slack"]

    monkeypatch.setattr(app_module.notify, "deliver", deliver)
    r = client.post("/api/settings/test-remind", headers=auth(admin))
    assert r.status_code == 200 and r.json() == {"sent": ["slack"], "failed": []}
    text, subject = sent[0]
    assert subject == "[テスト] 返事をしていないメールのお知らせ"
    assert text.startswith("[テスト] 返事をしていないメールのお知らせは、ここに届きます（boss")
    # 待ち時間に関係なく今の未対応のメールを並べ、外から来た文字は無害にする
    assert "さっき届いた ＜!here＞" in text and "<!here>" not in text
    # 見本なので、お知らせ済みにはしない
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert _reminded(conn) == set()
    # メールの送り先も入れていて、Slack だけ届いたとき
    client.patch("/api/settings", headers=auth(admin),
                 json={"mail_to": "boss@example.com", "smtp_host": "smtp.example.com"})
    r = client.post("/api/settings/test-remind", headers=auth(admin))
    assert r.json() == {"sent": ["slack"], "failed": ["email"]}
    # どこにも送れなかった
    monkeypatch.setattr(app_module.notify, "deliver", lambda s, text, subject="": [])
    r = client.post("/api/settings/test-remind", headers=auth(admin))
    assert r.status_code == 502 and "送信できませんでした" in r.json()["detail"]


def test_test_remind_without_unhandled_mail(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    _mail(conn, 1, 3, status="replied")
    subject, text = mail.remind_test_message(conn, {"mail_page_url": "https://d.example.com/mail"},
                                             "管理者", now=NOW)
    assert "いまは「未対応」のメールはありません。" in text
    assert "件名1" not in text and "受信箱を開く: https://d.example.com/mail" in text
    conn.close()


# ---------- 定期受信の回で動く ----------

def test_background_tick_reminds_even_when_fetch_fails(client, users, monkeypatch):
    from server import app as app_module
    calls = []
    monkeypatch.setattr(app_module.mail, "remind_unanswered",
                        lambda conn, **kw: calls.append(conn) or 0)

    def broken(conn, **kw):
        raise OSError("受信サーバにつながらない")

    monkeypatch.setattr(app_module.mail, "check_mail", broken)
    with pytest.raises(OSError):        # 受信の失敗はこれまでどおりループのログに出る
        app_module._mail_tick()
    assert len(calls) == 1

    # お知らせが失敗しても、受信・保存日数の片付けは止めない (例外にしない)
    def remind_broken(conn, **kw):
        raise RuntimeError("お知らせの失敗")

    monkeypatch.setattr(app_module.mail, "check_mail", lambda conn, **kw: 0)
    monkeypatch.setattr(app_module.mail, "remind_unanswered", remind_broken)
    app_module._mail_tick()


def test_background_tick_sends_reminder(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    from server import db
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": SLACK, "mail_remind_quiet": ""})
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mid = _mail(conn, 1, 3, priority=1, now=datetime.now(timezone.utc))
    texts = []
    monkeypatch.setattr(app_module.notify, "send_slack", lambda url, text: texts.append(text))
    app_module._mail_tick()             # IMAP 未設定でも、届いているメールのお知らせはする
    app_module._mail_tick()
    assert len(texts) == 1 and "件名1" in texts[0]
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert _reminded(conn) == {mid}


# ---------- 前からある DB・画面 ----------

def test_existing_db_gets_reminded_at(tmp_path):
    from server import db
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE mails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL UNIQUE, imap_uid INTEGER,
        from_addr TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '',
        to_addr TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
        priority_reason TEXT NOT NULL DEFAULT '',
        priority_source TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unhandled',
        draft_reply TEXT NOT NULL DEFAULT '', draft_source TEXT NOT NULL DEFAULT '',
        draft_generated_at TEXT, reply_subject TEXT NOT NULL DEFAULT '',
        reply_body TEXT NOT NULL DEFAULT '', replied_at TEXT,
        replied_by TEXT NOT NULL DEFAULT '', references_hdr TEXT NOT NULL DEFAULT '')""")
    old.execute("INSERT INTO mails (message_id, subject, received_at, fetched_at)"
                " VALUES ('<o@e>', '前のメール', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')")
    old.commit()
    old.close()
    for _ in range(2):   # 何度開いても壊れない
        conn = db.connect(path)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(mails)")]
        assert cols.count("reminded_at") == 1
        assert not conn.in_transaction
        conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT reminded_at FROM mails").fetchone()[0] is None
    conn.close()


def test_docker_compose_passes_domain_to_app():
    from pathlib import Path
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
        encoding="utf-8")
    app_part = compose[compose.index("  app:"):compose.index("  caddy:")]
    for line in ("ZATSUMU_DOMAIN: ${ZATSUMU_DOMAIN:-}",
                 "ZATSUMU_MAIL_DOMAIN: ${ZATSUMU_MAIL_DOMAIN:-}",
                 "ZATSUMU_CADDYFILE: ${ZATSUMU_CADDYFILE:-}"):
        assert line in app_part, line


def test_mail_page_has_remind_settings(client):
    page = client.get("/mail").text
    for marker in ('id="remind-card"', 'id="set-remind-enabled"', 'id="set-remind-high"',
                   'id="set-remind-normal"', 'id="set-remind-quiet-from"',
                   'id="set-remind-quiet-to"', 'id="set-mail-to"', 'id="set-slack"',
                   'id="set-mail-page-url"', 'id="set-test-remind"',
                   '"/api/settings/test-remind"', "mail_remind_quiet: quietValue(),"):
        assert marker in page, marker
    # Slack の URL は画面に出さず、入れたときだけ送る
    assert '$("set-slack").value = "";' in page
    assert 'if ($("set-slack").value.trim()) body.slack_webhook_url' in page
    # 通知先メールは勤怠と共通なので、画面で変えたときだけ送る
    assert '["mail_to", "set-mail-to"]]' in page
    # サーバから来た文字は textContent で出す
    assert '$("remind-dest")' in page and "dest.textContent" in page
    assert 'msg.textContent = e.detail' in page
