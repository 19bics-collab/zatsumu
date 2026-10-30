"""週ごとのまとめのテスト.

- 数え方 (届いた数・優先度・種類・返信した数と返信までの時間・対応不要にした数・
  いま未対応のメール・よく届いた差出人・AI の利用) と、空の週
- 期間の区切り (集計のタイムゾーン=日本時間の 0時。送る日の前の7日間)
- 決めた曜日・時刻に1回だけ送る (再起動しても二度送らない・送れなければもう一度・
  その日のうちに送れなかった週は送らない・曜日を変えた直後に続けて送らない)
- 見本・「今すぐ送る」の API、設定、対応不要にした時刻、前からある DB、画面

ネットワークには出ない (送信は偽物に差し替える)。
"""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.test_api import auth

SLACK = "https://hooks.slack.test/T000/B000/secret-path"
JST = timezone(timedelta(hours=9))
# 2026-10-19 は月曜。既定 (月曜 9時) なら、前の週 10/12(月)〜10/18(日) の分を送る
MONDAY = date(2026, 10, 19)
ON = {"mail_report_enabled": 1, "mail_report_weekday": 0, "mail_report_hour": 9}


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


def _jst(y, mo, d, h=0, mi=0, s=0):
    """日本時間の日時を UTC で."""
    return datetime(y, mo, d, h, mi, s, tzinfo=JST).astimezone(timezone.utc)


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _conn(tmp_path, **settings):
    from server import db
    conn = db.connect(tmp_path / "zatsumu.db")
    for key, value in {"slack_webhook_url": SLACK, **settings}.items():
        db.set_setting(conn, key, value)
    conn.commit()
    return conn


def _mail(conn, n, fetched, priority=2, status="unhandled", **kw):
    cur = conn.execute(
        "INSERT INTO mails (message_id, from_addr, from_name, subject, received_at,"
        " fetched_at, priority, priority_source, status, category, replied_at,"
        " draft_source, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"<w{n}@example.com>", kw.get("from_addr", f"guest{n}@example.com"),
         kw.get("from_name", ""), kw.get("subject", f"件名{n}"),
         # 差出人が付ける日付はずれていても数え方に関係ない (取り込んだ時刻で数える)
         "2020-01-01T00:00:00+00:00", _iso(fetched), priority,
         kw.get("priority_source", "rule"), status, kw.get("category", ""),
         _iso(kw["replied"]) if kw.get("replied") else None,
         kw.get("draft_source", ""),
         _iso(kw["archived"]) if kw.get("archived") else None))
    conn.commit()
    return cur.lastrowid


def _week_fixture(conn):
    """10/12(月)〜10/18(日) の週の前後にメールを入れる (期間の境目も含む)."""
    yama, news = "Yamamoto@Example.com", "newsletter@mail.example.com"
    # 期間の最初 (月曜 0時ちょうど) は入る。2時間で返信 (AI の判定・AI の下書き)
    _mail(conn, 1, _jst(2026, 10, 12, 0, 0), priority=1, status="replied",
          category="清掃のご依頼", from_addr=yama, from_name="山本",
          replied=_jst(2026, 10, 12, 2, 0), priority_source="ai", draft_source="ai")
    # 30分で返信 (定型文の下書き)
    _mail(conn, 2, _jst(2026, 10, 13, 10, 0), status="replied", category="清掃のご依頼",
          from_addr="yamamoto@example.com", replied=_jst(2026, 10, 13, 10, 30),
          draft_source="template")
    # 2日後に返信。名前は一番新しいメールのもの
    _mail(conn, 3, _jst(2026, 10, 14, 9, 0), status="replied",
          category="予約・宿泊のお問い合わせ", from_addr="yamamoto@example.com",
          from_name="山本 商事", replied=_jst(2026, 10, 16, 9, 0), priority_source="ai")
    # 期間の最後 (日曜 23:59:59) は入る。まだ未対応 (一番長く待っている)
    _mail(conn, 4, _jst(2026, 10, 18, 23, 59, 59), priority=3, from_addr=news)
    # 次の月曜 0時ちょうどは入らない (いま未対応の数には入る)
    _mail(conn, 5, _jst(2026, 10, 19, 0, 0), priority=1, category="清掃のご依頼")
    # 前の週に届いて、この週に返信した (届いた数には入らず、返信した数と時間には入る)
    _mail(conn, 6, _jst(2026, 10, 11, 21, 0), status="replied",
          replied=_jst(2026, 10, 12, 7, 0))
    # この週に届いて、この週に対応不要にした
    _mail(conn, 7, _jst(2026, 10, 15, 11, 0), priority=3, status="archived",
          category="営業・宣伝", from_addr=news, archived=_jst(2026, 10, 15, 12, 0))
    # 前の週に対応不要にしたものは入らない
    _mail(conn, 8, _jst(2026, 10, 9, 9, 0), status="archived", archived=_jst(2026, 10, 10, 9))
    # この週に対応不要にしたが、後で未対応に戻したものは「対応不要にした」に入らない
    _mail(conn, 9, _jst(2026, 10, 20, 9, 0), archived=_jst(2026, 10, 15, 9))
    # この週に届いて、次の月曜 0時ちょうどに返信した (返信した数には入らない)
    _mail(conn, 10, _jst(2026, 10, 18, 20, 0), status="replied", category="清掃のご依頼",
          from_addr="tanaka@example.com", replied=_jst(2026, 10, 19, 0, 0))


# ---------- 数え方 ----------

def test_weekly_stats_on_fixture_week(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    _week_fixture(conn)
    start, end = mail.report_period(MONDAY)
    now = _jst(2026, 10, 19, 9, 0)
    st = mail.weekly_stats(conn, start, end, now)
    assert st["received"] == 6                       # 1, 2, 3, 4, 7, 10
    assert st["by_priority"] == {1: 1, 2: 3, 3: 2}
    assert st["by_category"] == {"清掃のご依頼": 3, "予約・宿泊のお問い合わせ": 1,
                                 "": 1, "営業・宣伝": 1}
    # 同じ差出人 (大文字・小文字は区別しない) から2通以上のものだけ、多い順
    assert [(s["addr"], s["name"], s["count"]) for s in st["top_senders"]] == [
        ("Yamamoto@Example.com", "山本 商事", 3),
        ("newsletter@mail.example.com", "", 2)]
    assert st["replied"] == 4                        # 1, 2, 3, 6
    # 返信までの時間: 2時間・30分・48時間・10時間
    assert st["reply_median_sec"] == 6 * 3600
    assert st["reply_average_sec"] == (120 + 30 + 2880 + 600) / 4 * 60
    assert st["archived"] == 1                       # 7
    assert (st["unhandled"], st["unhandled_high"]) == (3, 1)   # 4, 5, 9
    assert st["oldest_wait_sec"] == 9 * 3600 + 1     # 4 (日曜 23:59:59) から月曜 9時まで
    assert (st["ai_classified"], st["ai_drafted"]) == (2, 1)
    conn.close()


def test_weekly_report_text(tmp_path):
    from server import mail
    conn = _conn(tmp_path, mail_page_url="https://desk.example.com/mail", **ON)
    _week_fixture(conn)
    subject, text = mail.weekly_report(conn, _settings(conn), _jst(2026, 10, 19, 9, 0), MONDAY)
    assert subject == "メールの週ごとのまとめ（10/12〜10/18）"
    lines = text.split("\n")
    assert lines[0] == "📊 メールの週ごとのまとめ（10/12(月)〜10/18(日)）"
    for line in (
            "■ 届いたメール: 6通",
            "・優先度: 高 1通／中 3通／低 2通",
            # 多い順。同じ数なら設定の順で、未分類は後ろ
            "・種類: 清掃のご依頼 3通／予約・宿泊のお問い合わせ 1通／営業・宣伝 1通／未分類 1通",
            # アドレスは縮める
            "・よく届いた差出人: 山本 商事（Yama…@Example.com） 3通／news…@mail.example.com 2通",
            "■ この7日間に対応したメール",
            "・返信した: 4通（返信までの時間: 半分のメールは6時間以内、平均は15時間8分）",
            "・対応不要にした: 1通",
            "■ いま未対応のメール: 3通（うち優先度[高] 1通。一番長く待っているメールは、届いてから9時間）",
            "■ AI の利用: AI が優先度と種類を判定したメール 2通／返信したメールのうち AI の下書きがあったもの 1通",
            "受信箱を開く: https://desk.example.com/mail",
            "（毎週月曜の9時に、その前の7日間の分をお送りしています）"):
        assert line in lines, line
    # 件名・本文の中身は載せない (数だけ)
    assert "件名1" not in text
    conn.close()


def test_empty_week(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    subject, text = mail.weekly_report(conn, _settings(conn), _jst(2026, 10, 19, 9, 0), MONDAY)
    assert subject == "メールの週ごとのまとめ（10/12〜10/18）"
    assert text == "\n".join([
        "📊 メールの週ごとのまとめ（10/12(月)〜10/18(日)）",
        "",
        "■ 届いたメール: 0通",
        "",
        "■ この7日間に対応したメール",
        "・返信した: 0通",
        "・対応不要にした: 0通",
        "",
        "■ いま未対応のメールはありません",
    ])
    st = mail.weekly_stats(conn, *mail.report_period(MONDAY), _jst(2026, 10, 19, 9, 0))
    assert st["reply_median_sec"] is None and st["oldest_wait_sec"] is None
    conn.close()


def test_outside_text_is_sanitized(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    for n in (1, 2):
        _mail(conn, n, _jst(2026, 10, 13, 9, n), from_addr="<!channel>evil@example.com",
              from_name="<!here>\n悪い\x07人", category="<b>種類</b>")
    _, text = mail.weekly_report(conn, _settings(conn), _jst(2026, 10, 19, 9, 0), MONDAY)
    assert "<" not in text and ">" not in text and "\x07" not in text
    assert "＜!here＞ 悪い人（＜!ch…@example.com） 2通" in text
    assert "・種類: ＜b＞種類＜/b＞ 2通" in text
    conn.close()


def test_many_categories_are_summarized(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    for n in range(1, 11):
        _mail(conn, n, _jst(2026, 10, 13, 9, n), category=f"種類{n:02d}")
    _, text = mail.weekly_report(conn, _settings(conn), _jst(2026, 10, 19, 9, 0), MONDAY)
    line = next(x for x in text.split("\n") if x.startswith("・種類: "))
    assert line.count("通／") == mail.REPORT_CATEGORY_MAX and line.endswith("／ほか 2通")
    conn.close()


def test_short_address_and_duration_text():
    from server import mail
    assert mail.short_address("yamamoto@example.com") == "yama…@example.com"
    assert mail.short_address("info@example.com") == "info@example.com"
    assert mail.short_address(" a b@x.jp ") == "ab@x.jp"
    assert mail.short_address("no-address") == "no-address"
    assert mail.short_address("") == ""
    d = mail._duration_text
    assert [d(10), d(59 * 60), d(3600), d(2.5 * 3600), d(2.5 * 3600 + 10),
            d(25 * 3600), d(48 * 3600), d(47.5 * 3600)] == [
        "1分", "59分", "1時間", "2時間30分", "2時間31分", "1日1時間", "2日", "2日"]


# ---------- 期間の区切り (日本時間) ----------

def test_report_period_follows_local_midnight():
    from server import mail, tz
    start, end = mail.report_period(MONDAY)
    assert (_iso(start), _iso(end)) == ("2026-10-11T15:00:00+00:00",
                                        "2026-10-18T15:00:00+00:00")
    assert mail.day_label(MONDAY) == "10/19(月)"
    # 集計のタイムゾーン (設定) を変えたら、その 0時で区切る
    tz.set_tz("UTC")
    start, end = mail.report_period(MONDAY)
    assert (_iso(start), _iso(end)) == ("2026-10-12T00:00:00+00:00",
                                        "2026-10-19T00:00:00+00:00")


def test_preview_period_is_the_7_days_before_today(tmp_path):
    from server import mail
    conn = _conn(tmp_path)
    # 日本時間の水曜 0:30 (UTC では火曜) なら、前の水曜〜火曜
    subject, _ = mail.weekly_report(conn, _settings(conn), _jst(2026, 10, 21, 0, 30))
    assert subject == "メールの週ごとのまとめ（10/14〜10/20）"
    conn.close()


# ---------- 決めた曜日・時刻に1回だけ ----------

def test_report_due_at_the_configured_time():
    from server import mail
    due = mail.report_due
    assert due(ON, _jst(2026, 10, 19, 8, 59), "") is None
    assert due(ON, _jst(2026, 10, 19, 9, 0), "") == MONDAY
    assert due(ON, _jst(2026, 10, 19, 23, 59), "") == MONDAY   # その日のうちなら後からでも
    assert due(ON, _jst(2026, 10, 20, 0, 0), "") is None       # 翌日になったらその週は送らない
    assert due(ON, _jst(2026, 10, 22, 12, 0), "") is None
    # 送ったら、その週はもう送らない (時刻を変えても)
    assert due(ON, _jst(2026, 10, 19, 10, 0), "2026-10-19") is None
    assert due({**ON, "mail_report_hour": 15}, _jst(2026, 10, 19, 16, 0), "2026-10-19") is None
    # 次の週は送る
    assert due(ON, _jst(2026, 10, 26, 9, 0), "2026-10-19") == date(2026, 10, 26)
    # 送る設定がオフ
    assert due({**ON, "mail_report_enabled": 0}, _jst(2026, 10, 19, 9, 0), "") is None
    # 日曜 0時 (日本時間。UTC では土曜) も
    sun = {**ON, "mail_report_weekday": 6, "mail_report_hour": 0}
    assert due(sun, _jst(2026, 10, 24, 23, 59), "") is None
    assert due(sun, _jst(2026, 10, 25, 0, 0), "") == date(2026, 10, 25)
    # 読めない記録は無いのと同じ・範囲の外の設定は既定 (月曜 9時)
    assert due(ON, _jst(2026, 10, 19, 9, 0), "壊れた値") == MONDAY
    assert due({**ON, "mail_report_weekday": 9, "mail_report_hour": 99},
               _jst(2026, 10, 19, 9, 0), "") == MONDAY


def test_changing_the_weekday_does_not_send_twice_in_a_row():
    from server import mail
    tue = {**ON, "mail_report_weekday": 1}
    # 月曜に送った後で火曜に変えても、翌日の火曜には送らず、次の週の火曜から
    assert mail.report_due(tue, _jst(2026, 10, 20, 10, 0), "2026-10-19") is None
    assert mail.next_report_at(tue, _jst(2026, 10, 20, 10, 0), "2026-10-19") == \
        _jst(2026, 10, 27, 9, 0)
    assert mail.report_due(tue, _jst(2026, 10, 27, 9, 0), "2026-10-19") == date(2026, 10, 27)
    # 金曜に変えたら、その週の金曜から (前に送った日から4日以上あいている)
    fri = {**ON, "mail_report_weekday": 4}
    assert mail.report_due(fri, _jst(2026, 10, 23, 9, 0), "2026-10-19") == date(2026, 10, 23)


def test_next_report_at():
    from server import mail
    nxt = mail.next_report_at
    assert nxt(ON, _jst(2026, 10, 19, 8, 0), "") == _jst(2026, 10, 19, 9, 0)
    # 時刻を過ぎてまだ送っていない (このあとの回で送る)
    assert nxt(ON, _jst(2026, 10, 19, 9, 30), "") == _jst(2026, 10, 19, 9, 0)
    assert nxt(ON, _jst(2026, 10, 19, 9, 30), "2026-10-19") == _jst(2026, 10, 26, 9, 0)
    assert nxt(ON, _jst(2026, 10, 22, 12, 0), "") == _jst(2026, 10, 26, 9, 0)
    assert nxt({**ON, "mail_report_enabled": 0}, _jst(2026, 10, 19, 8, 0), "") is None


def test_send_weekly_report_once_per_week(tmp_path):
    from server import mail
    conn = _conn(tmp_path, **ON)
    _mail(conn, 1, _jst(2026, 10, 14, 9, 0))
    n = Notifier()
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 19, 8, 59)) is False
    assert n.calls == []
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 19, 9, 0)) is True
    assert n.calls[0]["subject"] == "メールの週ごとのまとめ（10/12〜10/18）"
    assert "■ 届いたメール: 1通" in n.calls[0]["text"]
    assert mail.report_last_sent(conn) == "2026-10-19"
    assert not conn.in_transaction
    # 次の回にはもう送らない。再起動しても (別の接続でも) 送らない
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 19, 9, 5)) is False
    conn.close()
    conn = _conn(tmp_path)
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 19, 15, 0)) is False
    assert len(n.calls) == 1
    # 次の週
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 26, 9, 10)) is True
    assert n.calls[1]["subject"] == "メールの週ごとのまとめ（10/19〜10/25）"
    assert mail.report_last_sent(conn) == "2026-10-26"
    conn.close()


def test_send_failure_is_retried_the_same_day(tmp_path):
    from server import mail
    conn = _conn(tmp_path, **ON)
    broken = Notifier(result=())         # Slack もメールも送れなかった
    assert mail.send_weekly_report(conn, notifier=broken, now=_jst(2026, 10, 19, 9, 0)) is False
    assert len(broken.calls) == 1 and mail.report_last_sent(conn) == ""
    ok = Notifier()
    assert mail.send_weekly_report(conn, notifier=ok, now=_jst(2026, 10, 19, 9, 5)) is True
    assert mail.report_last_sent(conn) == "2026-10-19"
    conn.close()


def test_no_channels_or_disabled_does_nothing(tmp_path):
    from server import db, mail
    conn = _conn(tmp_path, **ON)
    db.set_setting(conn, "slack_webhook_url", "")
    db.set_setting(conn, "mail_to", "boss@example.com")   # SMTP が無いのでメールにも送れない
    conn.commit()
    n = Notifier()
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 19, 9, 0)) is False
    assert n.calls == [] and mail.report_last_sent(conn) == ""
    db.set_setting(conn, "slack_webhook_url", SLACK)
    db.set_setting(conn, "mail_report_enabled", 0)
    conn.commit()
    assert mail.send_weekly_report(conn, notifier=n, now=_jst(2026, 10, 19, 9, 0)) is False
    assert n.calls == [] and mail.report_last_sent(conn) == ""
    conn.close()


def test_uses_notify_deliver_by_default(tmp_path, monkeypatch):
    from server import mail, notify
    conn = _conn(tmp_path, **ON)
    sent = []
    monkeypatch.setattr(notify, "send_slack", lambda url, text: sent.append((url, text)))
    assert mail.send_weekly_report(conn, now=_jst(2026, 10, 19, 9, 0)) is True
    assert sent[0][0] == SLACK and sent[0][1].startswith("📊 メールの週ごとのまとめ")
    conn.close()


# ---------- 設定・API ----------

def _settings(conn):
    from server import db
    return db.get_settings(conn)


def test_report_settings_defaults_and_validation(client, users, tmp_path):
    _, admin = users
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert (s["mail_report_enabled"], s["mail_report_weekday"], s["mail_report_hour"]) == \
        (0, 0, 9)
    assert s["mail_report_next_at"] is None
    assert not [k for k in s if k.startswith("mail_state_")]

    def patch(**body):
        return client.patch("/api/settings", headers=auth(admin), json=body)

    for key, bad, label in (("mail_report_weekday", -1, "送る曜日"),
                            ("mail_report_weekday", 7, "送る曜日"),
                            ("mail_report_hour", -1, "送る時刻"),
                            ("mail_report_hour", 24, "送る時刻")):
        r = patch(**{key: bad})
        assert r.status_code == 400 and label in r.json()["detail"], (key, bad)
    r = patch(mail_report_enabled=True, mail_report_weekday=4, mail_report_hour=17)
    assert r.status_code == 200
    s = r.json()
    assert (s["mail_report_enabled"], s["mail_report_weekday"], s["mail_report_hour"]) == \
        (1, 4, 17)
    # 次に送る日時 (金曜 17時。日本時間)
    nxt = datetime.fromisoformat(s["mail_report_next_at"]).astimezone(JST)
    assert (nxt.weekday(), nxt.hour, nxt.minute) == (4, 17, 0)
    # (今日が金曜で 17時を過ぎていれば、まだ送っていない今日の分。このあとの回で送る)
    assert -timedelta(days=1) < nxt - datetime.now(JST) <= timedelta(days=7)
    csv_text = client.get("/api/reports/audit.csv?month=" +
                          datetime.now().strftime("%Y-%m"), headers=auth(admin)).text
    assert "mail_report_weekday=4" in csv_text and "mail_report_hour=17" in csv_text


def test_preview_endpoint(client, users, tmp_path):
    from server import db, mail
    worker, admin = users
    assert client.get("/api/mail/report/preview", headers=auth(worker)).status_code == 403
    assert client.get("/api/mail/report/preview").status_code == 401
    now = datetime.now(timezone.utc)
    today = now.astimezone(JST).date()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        _mail(conn, 1, now - timedelta(days=2), priority=1, status="replied",
              replied=now - timedelta(days=2) + timedelta(hours=1))
        _mail(conn, 2, now)       # 今日届いたものは、まだ入らない (昨日まで)
    r = client.get("/api/mail/report/preview", headers=auth(admin))
    assert r.status_code == 200
    body = r.json()
    first, last = today - timedelta(days=7), today - timedelta(days=1)
    assert (body["start"], body["end"]) == (first.isoformat(), last.isoformat())
    assert body["subject"] == f"メールの週ごとのまとめ（{first.month}/{first.day}〜{last.month}/{last.day}）"
    assert "■ 届いたメール: 1通" in body["text"]
    assert "・返信した: 1通（返信までの時間: 半分のメールは1時間以内" in body["text"]
    assert "■ いま未対応のメール: 1通" in body["text"]
    assert body["next_at"] is None and body["last_sent_for"] is None
    assert body["channels"] == []
    # 見本を見ても、送ったことにはしない
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.report_last_sent(conn) == ""
    # 送る設定をオンにすると、次に送る日時と、いつ送っているかの説明が出る
    client.patch("/api/settings", headers=auth(admin),
                 json={"mail_report_enabled": True, "slack_webhook_url": SLACK})
    body = client.get("/api/mail/report/preview", headers=auth(admin)).json()
    assert body["next_at"] and body["channels"] == ["slack"]
    assert "（毎週月曜の9時に、その前の7日間の分をお送りしています）" in body["text"]


def test_preview_counts_mails_of_the_last_7_days(client, users, tmp_path):
    from server import db
    _, admin = users
    today = datetime.now(JST).date()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        # 7日前の 0時ちょうど (入る)・その直前 (入らない)・昨日の終わり (入る)
        d7, d1 = today - timedelta(days=7), today - timedelta(days=1)
        _mail(conn, 1, datetime(d7.year, d7.month, d7.day, tzinfo=JST))
        _mail(conn, 2, datetime(d7.year, d7.month, d7.day, tzinfo=JST) - timedelta(seconds=1))
        _mail(conn, 3, datetime(d1.year, d1.month, d1.day, 23, 59, 59, tzinfo=JST))
    text = client.get("/api/mail/report/preview", headers=auth(admin)).json()["text"]
    assert "■ 届いたメール: 2通" in text


def test_send_now_endpoint(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    from server import db, mail
    worker, admin = users
    r = client.post("/api/mail/report/send-now", headers=auth(admin))
    assert r.status_code == 400 and "送り先が未設定" in r.json()["detail"]
    assert client.post("/api/mail/report/send-now", headers=auth(worker)).status_code == 403
    client.patch("/api/settings", headers=auth(admin), json={"slack_webhook_url": SLACK})
    sent = []

    def deliver(s, text, subject="zatsumu 通知"):
        sent.append((text, subject))
        return ["slack"]

    monkeypatch.setattr(app_module.notify, "deliver", deliver)
    # 送る設定がオフでも送れる
    r = client.post("/api/mail/report/send-now", headers=auth(admin))
    assert r.status_code == 200 and r.json() == {"sent": ["slack"], "failed": []}
    text, subject = sent[0]
    assert subject.startswith("メールの週ごとのまとめ（")
    assert text.startswith("📊 メールの週ごとのまとめ")
    assert text.endswith("（bossが設定画面の「今すぐ送る」で送りました）")
    # 決まった曜日に送る分とは別 (送ったことにはしない)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.report_last_sent(conn) == ""
        detail = conn.execute("SELECT detail FROM audit_log WHERE action = 'mail_report_send'"
                              ).fetchone()["detail"]
    assert detail == "sent=slack"
    # メールの送り先も入れていて、Slack だけ届いたとき
    client.patch("/api/settings", headers=auth(admin),
                 json={"mail_to": "boss@example.com", "smtp_host": "smtp.example.com"})
    r = client.post("/api/mail/report/send-now", headers=auth(admin))
    assert r.json() == {"sent": ["slack"], "failed": ["email"]}
    # どこにも送れなかった
    monkeypatch.setattr(app_module.notify, "deliver", lambda s, text, subject="": [])
    r = client.post("/api/mail/report/send-now", headers=auth(admin))
    assert r.status_code == 502 and "送信できませんでした" in r.json()["detail"]


def test_report_routes_are_not_taken_as_mail_id(client, users):
    from server import app as app_module
    paths = [getattr(r, "path", "") for r in app_module.app.routes]
    first_param = paths.index("/api/mail/{mail_id}")
    for path in ("/api/mail/report/preview", "/api/mail/report/send-now"):
        assert paths.index(path) < first_param, path


# ---------- 対応不要にした時刻 ----------

def test_archiving_records_the_time(client, users, tmp_path):
    from server import db
    _, admin = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mid = _mail(conn, 1, datetime.now(timezone.utc))

    def archived_at():
        with db.get_db(tmp_path / "zatsumu.db") as conn:
            return conn.execute("SELECT archived_at FROM mails WHERE id = ?",
                                (mid,)).fetchone()[0]

    def patch(**body):
        r = client.patch(f"/api/mail/{mid}", headers=auth(admin), json=body)
        assert r.status_code == 200

    patch(priority=1)
    assert archived_at() is None
    before = datetime.now(timezone.utc)
    patch(status="archived")
    first = archived_at()
    assert first and datetime.fromisoformat(first) >= before
    # もう一度「対応不要」にしても (状態が変わらない)、時刻は変えない
    patch(status="archived", priority=2)
    assert archived_at() == first
    # 未対応に戻してからまた対応不要にしたら、新しい時刻
    patch(status="unhandled")
    patch(status="archived")
    assert archived_at() >= first


def test_existing_db_gets_archived_at_from_audit_log(tmp_path):
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
    old.execute("""CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
        action TEXT NOT NULL, target_user_id INTEGER, session_id INTEGER,
        detail TEXT, at TEXT NOT NULL)""")
    for n, status in ((1, "archived"), (2, "archived"), (3, "unhandled"), (12, "archived")):
        old.execute("INSERT INTO mails (id, message_id, received_at, fetched_at, status)"
                    " VALUES (?, ?, '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', ?)",
                    (n, f"<o{n}@e>", status))
    for detail, at in (
            ("mail=1 status=archived", "2026-10-13T01:00:00+00:00"),
            ("mail=1 status=unhandled", "2026-10-13T02:00:00+00:00"),
            ("mail=1 priority=1, status=archived", "2026-10-14T03:00:00+00:00"),
            ("mail=1 priority=3", "2026-10-15T03:00:00+00:00"),
            # 対応不要に戻したが、いまは未対応 (時刻は入れない)
            ("mail=3 status=archived", "2026-10-13T04:00:00+00:00"),
            # 番号の頭が同じ別のメール (mail=1 とまちがえない)
            ("mail=12 status=archived", "2026-10-16T05:00:00+00:00"),
            ("mail=12 category=status=archivedのような種類", "2026-10-17T05:00:00+00:00")):
        old.execute("INSERT INTO audit_log (admin_id, action, detail, at)"
                    " VALUES (1, 'mail_update', ?, ?)", (detail, at))
    old.execute("INSERT INTO audit_log (admin_id, action, detail, at) VALUES"
                " (1, 'mail_send', 'mail=2 status=archived', '2026-10-18T00:00:00+00:00')")
    old.commit()
    old.close()
    for _ in range(2):   # 何度開いても壊れない
        conn = db.connect(path)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(mails)")]
        assert cols.count("archived_at") == 1
        assert not conn.in_transaction
        conn.close()
    conn = db.connect(path)
    got = dict(conn.execute("SELECT id, archived_at FROM mails").fetchall())
    assert got == {1: "2026-10-14T03:00:00+00:00", 2: None, 3: None,
                   12: "2026-10-16T05:00:00+00:00"}
    conn.close()


# ---------- 定期受信の回で動く ----------

def test_background_tick_sends_report_even_when_fetch_fails(client, users, monkeypatch):
    from server import app as app_module
    calls = []
    monkeypatch.setattr(app_module.mail, "send_weekly_report",
                        lambda conn, **kw: calls.append(conn) or False)

    def broken(conn, **kw):
        raise OSError("受信サーバにつながらない")

    monkeypatch.setattr(app_module.mail, "check_mail", broken)
    with pytest.raises(OSError):        # 受信の失敗はこれまでどおりループのログに出る
        app_module._mail_tick()
    assert len(calls) == 1

    # まとめが失敗しても、ほかの処理は止めない (例外にしない)
    def report_broken(conn, **kw):
        raise RuntimeError("まとめの失敗")

    monkeypatch.setattr(app_module.mail, "check_mail", lambda conn, **kw: 0)
    monkeypatch.setattr(app_module.mail, "send_weekly_report", report_broken)
    app_module._mail_tick()


def test_background_tick_sends_report_once(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    _, admin = users
    # 今日の 0時に送る設定にする (いまは必ずその時刻を過ぎている)
    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": SLACK, "mail_report_enabled": True,
                       "mail_report_weekday": datetime.now(JST).weekday(),
                       "mail_report_hour": 0, "mail_remind_enabled": False})
    texts = []
    monkeypatch.setattr(app_module.notify, "send_slack", lambda url, text: texts.append(text))
    app_module._mail_tick()             # IMAP 未設定でも、まとめは送る
    app_module._mail_tick()
    assert len(texts) == 1 and texts[0].startswith("📊 メールの週ごとのまとめ")
    s = client.get("/api/settings", headers=auth(admin)).json()
    nxt = datetime.fromisoformat(s["mail_report_next_at"]).astimezone(JST)
    assert nxt.date() == datetime.now(JST).date() + timedelta(days=7)


# ---------- 画面 ----------

def test_mail_page_has_report_settings(client):
    page = client.get("/mail").text
    for marker in ('id="report-card"', 'id="set-report-enabled"', 'id="set-report-weekday"',
                   'id="set-report-hour"', 'id="set-report-preview"', 'id="set-report-send"',
                   'id="report-preview"', 'id="report-next"',
                   '"/api/mail/report/preview"', '"/api/mail/report/send-now"',
                   "mail_report_enabled: $(\"set-report-enabled\").checked,",
                   "mail_report_weekday: +$(\"set-report-weekday\").value,",
                   "mail_report_hour: +$(\"set-report-hour\").value,"):
        assert marker in page, marker
    # サーバから来た文 (見本・エラー) は textContent で出す
    assert "box.textContent = `件名: ${e.subject}" in page
    assert "el.textContent = text;" in page
    # 送り先が無いときは「送りますか？」と聞く前に知らせる
    i = page.index('$("set-report-send").onclick')
    send = page[i:page.index("};", i)]
    assert send.index("if (!names.length)") < send.index("confirm(")
