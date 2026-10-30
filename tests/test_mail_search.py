"""受信箱・送信箱の検索欄のテスト (server/mail_search.py).

- 日本語の言葉の途中 (1文字・2文字・3文字以上) で、件名・本文・相手・宛先・添付の名前から見つかる
- 空白 (全角も) で区切った言葉は全部を含むメールだけ。英字の大文字・小文字・全角の英数字
- 取引先の名前 (名前を付けた取引先) で、その取引先の相手のメールが見つかる
- 状態・タブ (種類)・担当・アドレスの絞り込みと一緒に効く。状態を送らなければ返信済み・対応不要も探す
- 探す言葉を SQL・索引の探し方に入れない (記号・SQL のような言葉でも、その文字を探すだけ)
- 長すぎる・多すぎる言葉は 400。権限 (勤怠だけの人は使えない)
- 索引 (FTS5 trigram) と、索引が使えないとき (1通ずつ見る) で同じ結果
- 索引はトリガーでいつも合う (変えた・消した・添付・前の版のコードが入れたメールも)
- 前からあるメールは少しずつ索引に入れ、入れ終わるまでは1通ずつ見て探す
- 5万通でも速い (索引を使う・500通を超えるときだけ数える)
- 画面に検索欄があり、印は esc() してから付ける
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

ROOT = Path(__file__).resolve().parent.parent
# 本番で今動いている版 (自動更新で戻ることがある版)
PREVIOUS = "aa00391"


@pytest.fixture(autouse=True)
def reset_tz():
    from server import tz
    tz.set_tz("Asia/Tokyo")
    yield
    tz.set_tz("Asia/Tokyo")


@pytest.fixture(params=["index", "scan"])
def mode(request, monkeypatch):
    """索引で探す (FTS5 trigram) か、索引が使えない SQLite (1通ずつ見る) か."""
    from server import mail_search
    if request.param == "scan":
        monkeypatch.setattr(mail_search, "_fts_ok", False)
    else:
        assert mail_search.fts_supported(), "この SQLite では FTS5 trigram が使えません"
    return request.param


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
    """勤怠だけの人・メール担当者・管理者."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        staff = db.create_user(conn, "suzuki")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        admin = db.create_user(conn, "boss", is_admin=True)
    return worker, staff, admin


def run_sql(tmp_path, sql, params=()):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return conn.execute(sql, params).lastrowid


def search(client, user, q, **params):
    r = client.get("/api/mail", headers=auth(user), params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r


def subjects(client, user, q, **params):
    return sorted(m["subject"] for m in search(client, user, q, **params).json())


@pytest.fixture()
def mails(tmp_path, users):
    """探す相手のメール (件名・本文・相手・宛先・CC・添付・状態がいろいろ)."""
    ids = {}
    ids["key"] = _add_mail(tmp_path, n=1, subject="鍵の受け渡しについて",
                           body="清掃は10月20日にお願いします。", from_addr="taro@abc.co.jp",
                           from_name="山田 太郎")
    ids["checkin"] = _add_mail(tmp_path, n=2, subject="チェックインのご案内",
                               body="鍵はポストに入れてあります", from_addr="hana@gmail.com",
                               from_name="Hanako SATO", status="replied")
    ids["invoice"] = _add_mail(tmp_path, n=3, subject="請求書", body="11月分の清掃費用です",
                               from_addr="keiri@xyz.co.jp", from_name="経理部", status="archived")
    ids["sale"] = _add_mail(tmp_path, n=4, subject="100% 割引_キャンペーン", body="配信停止はこちら",
                            from_addr="news@shop.example", from_name="ショップ")
    ids["pending"] = _add_mail(tmp_path, n=5, subject="清掃の日程", body="11月の清掃日程です",
                               from_addr="jiro@abc.co.jp", from_name="鈴木 次郎",
                               status="pending_approval")
    run_sql(tmp_path, "INSERT INTO mail_attachments (mail_id, filename, created_at)"
                      " VALUES (?, '見積書_10月.pdf', 'x')", (ids["invoice"],))
    run_sql(tmp_path, "UPDATE mails SET cc_addr = 'shimizu@cc-partner.example', category = '清掃のご依頼'"
                      " WHERE id = ?", (ids["key"],))
    return ids


# ---------- 言葉の途中・AND・大文字小文字 ----------

def test_japanese_substrings_of_any_length(client, users, mails, mode):
    _, _, admin = users
    # 1文字 (件名と本文)
    assert subjects(client, admin, "鍵") == ["チェックインのご案内", "鍵の受け渡しについて"]
    # 2文字
    assert subjects(client, admin, "清掃") == ["清掃の日程", "請求書", "鍵の受け渡しについて"]
    # 3文字以上 (言葉の途中)
    assert subjects(client, admin, "ェックイ") == ["チェックインのご案内"]
    assert subjects(client, admin, "受け渡し") == ["鍵の受け渡しについて"]
    # 差出人の名前・アドレス・CC・添付ファイルの名前
    assert subjects(client, admin, "山田") == ["鍵の受け渡しについて"]
    assert subjects(client, admin, "keiri@xyz") == ["請求書"]
    assert subjects(client, admin, "cc-partner") == ["鍵の受け渡しについて"]
    assert subjects(client, admin, "見積書") == ["請求書"]
    r = search(client, admin, "見積書").json()
    assert r[0]["match_files"] == ["見積書_10月.pdf"]
    # 合わない言葉
    assert subjects(client, admin, "存在しない言葉") == []
    assert search(client, admin, "存在しない言葉").headers["X-Search-Total"] == "0"
    assert search(client, admin, "鍵").headers["X-Search-Mode"] == mode


def test_terms_are_anded_and_case_and_width_insensitive(client, users, mails, mode):
    _, _, admin = users
    assert subjects(client, admin, "清掃 10月") == ["請求書", "鍵の受け渡しについて"]
    # 全角の空白で区切っても同じ。1つでも合わなければ出ない
    assert subjects(client, admin, "清掃　11月") == ["清掃の日程", "請求書"]
    assert subjects(client, admin, "清掃 11月 経理") == ["請求書"]
    assert subjects(client, admin, "清掃 鍵 チェックイン") == []
    # 英字の大文字・小文字、全角の英数字 (日本語入力のまま打った言葉)
    assert subjects(client, admin, "hanako sato") == ["チェックインのご案内"]
    assert subjects(client, admin, "ＡＢＣ") == ["清掃の日程", "鍵の受け渡しについて"]
    assert subjects(client, admin, "１０月") == ["請求書", "鍵の受け渡しについて"]
    # 同じ言葉を2回書いても1つ
    assert subjects(client, admin, "鍵 鍵") == subjects(client, admin, "鍵")


def test_snippet_is_around_the_first_match(client, users, tmp_path, mode):
    _, _, admin = users
    body = "いつもお世話になっております。" * 20 + "玄関の暗証番号は1234です。" + "よろしくお願いします。" * 20
    _add_mail(tmp_path, n=1, subject="ご案内", body=body)
    m = search(client, admin, "暗証番号").json()[0]
    assert "玄関の暗証番号は1234です" in m["snippet"]
    assert m["snippet"].startswith("…") and m["snippet"].endswith("…")
    assert len(m["snippet"]) <= 82
    # 件名だけに合うときは、抜き出しは空
    assert search(client, admin, "ご案内").json()[0]["snippet"] == ""


def test_client_name_finds_mails_of_that_client(client, users, mails, mode):
    from server import clients, db
    _, _, admin = users
    r = client.post("/api/mail/clients", headers=auth(admin),
                    json={"name": "ABC商事", "patterns": ["@abc.co.jp"]})
    assert r.status_code == 200, r.text
    assert subjects(client, admin, "ABC商事") == ["清掃の日程", "鍵の受け渡しについて"]
    assert subjects(client, admin, "商事 鍵") == ["鍵の受け渡しについて"]
    item = search(client, admin, "商事").json()[0]
    assert item["client_name"] == "ABC商事"
    # 取引先から1人だけ別の取引先に分けたら、その人のメールは出ない
    with db.get_db(Path(os.environ["ZATSUMU_DATA_DIR"]) / "zatsumu.db") as conn:
        g = clients.Grouping(conn)
        clients.create_client(conn, g, "次郎さん", patterns=["jiro@abc.co.jp"], move=True)
    assert subjects(client, admin, "ABC商事") == ["鍵の受け渡しについて"]


# ---------- ほかの絞り込みと一緒に ----------

def test_search_works_with_status_tab_assignee_and_account(client, users, mails, tmp_path, mode):
    _, staff, admin = users
    # 状態を送らなければ、返信済み・対応不要・確認待ちも探す
    assert subjects(client, admin, "清掃") == ["清掃の日程", "請求書", "鍵の受け渡しについて"]
    assert subjects(client, admin, "清掃", status="unhandled") == ["鍵の受け渡しについて"]
    assert subjects(client, admin, "清掃", status="archived") == ["請求書"]
    assert subjects(client, admin, "清掃", status="pending_approval") == ["清掃の日程"]
    # タブ (種類)
    assert subjects(client, admin, "清掃", category="清掃のご依頼") == ["鍵の受け渡しについて"]
    assert subjects(client, admin, "清掃", category="none") == ["清掃の日程", "請求書"]
    # 担当
    run_sql(tmp_path, "UPDATE mails SET assignee_user_id = ? WHERE id = ?", (staff["id"], mails["invoice"]))
    assert subjects(client, staff, "清掃", assignee="me") == ["請求書"]
    assert subjects(client, admin, "清掃", assignee="none") == ["清掃の日程", "鍵の受け渡しについて"]
    # 届いたアドレス
    acc = run_sql(tmp_path, "INSERT INTO mail_accounts (label, created_at) VALUES ('info', 'x')")
    run_sql(tmp_path, "UPDATE mails SET account_id = ? WHERE id = ?", (acc, mails["pending"]))
    assert subjects(client, admin, "清掃", account=str(acc)) == ["清掃の日程"]
    # 重要度順でも探せる
    r = search(client, admin, "清掃", sort="priority")
    assert len(r.json()) == 3


def test_more_than_500_hits_are_counted(client, users, tmp_path, mode):
    from server import db
    _, _, admin = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.executemany(
            "INSERT INTO mails (message_id, from_addr, subject, body, received_at, fetched_at)"
            " VALUES (?, 'a@b.example', ?, '本文', ?, ?)",
            [(f"<m{i}@x>", f"清掃のご依頼 {i}", f"2026-09-01T00:{i % 60:02d}:00+00:00",
              f"2026-09-01T00:{i % 60:02d}:00+00:00") for i in range(620)])
    r = search(client, admin, "清掃")
    assert len(r.json()) == 500 and r.headers["X-Search-Total"] == "620"
    r = search(client, admin, "ご依頼 619")
    assert [m["subject"] for m in r.json()] == ["清掃のご依頼 619"]
    assert r.headers["X-Search-Total"] == "1"


# ---------- 探す言葉の扱い (SQL に入れない・長さ) ----------

@pytest.mark.parametrize("q", [
    "'; DROP TABLE mails; --", '"', '" OR 1=1 --', "*", "NEAR(a b)", "{subject}:x", "a OR b",
    "^鍵", "鍵*", "(", ")", "\\", "%%", "' OR ''='", "-鍵", "subject:鍵",
])
def test_query_is_never_sql_or_index_syntax(client, users, mails, tmp_path, mode, q):
    _, _, admin = users
    r = client.get("/api/mail", headers=auth(admin), params={"q": q})
    assert r.status_code == 200, r.text
    # どれも、その文字を含むメールは無い (記号を探すだけ。全部のメールが出たりしない)
    assert r.json() == []
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 5


def test_symbols_are_searched_as_plain_characters(client, users, mails, mode):
    _, _, admin = users
    # 制御文字 (改行・タブ・NUL) は区切り
    assert subjects(client, admin, "鍵\x00受け渡し\n") == ["鍵の受け渡しについて"]
    # LIKE の % と _ は、その文字そのもの
    assert subjects(client, admin, "%") == ["100% 割引_キャンペーン"]
    assert subjects(client, admin, "_") == ["100% 割引_キャンペーン", "請求書"]
    assert subjects(client, admin, "100%") == ["100% 割引_キャンペーン"]


def test_query_limits_and_permissions(client, users, mails):
    worker, staff, admin = users
    assert client.get("/api/mail", headers=auth(admin), params={"q": "あ" * 101}).status_code == 400
    many = " ".join(f"w{i}" for i in range(11))
    r = client.get("/api/mail", headers=auth(admin), params={"q": many})
    assert r.status_code == 400 and "10個" in r.json()["detail"]
    assert client.get("/api/mail", headers=auth(admin), params={"q": "あ" * 100}).status_code == 200
    # 空白だけなら探さない (ふつうの一覧)
    r = client.get("/api/mail", headers=auth(admin), params={"q": " 　 "})
    assert r.status_code == 200 and "X-Search-Total" not in r.headers
    # メール担当者は探せる。勤怠だけの人は使えない
    assert client.get("/api/mail", headers=auth(staff), params={"q": "鍵"}).status_code == 200
    assert client.get("/api/mail", headers=auth(worker), params={"q": "鍵"}).status_code == 403
    assert client.get("/api/mail/sent", headers=auth(worker), params={"q": "鍵"}).status_code == 403
    assert client.get("/api/mail/sent", headers=auth(staff),
                      params={"q": "あ" * 101}).status_code == 400


# ---------- 送信箱 ----------

def test_sent_box_search(client, users, tmp_path, mode):
    _, staff, admin = users
    # この画面から送った返信 (返信の件名・本文・宛先・CC で探す)
    a = _add_mail(tmp_path, n=1, subject="鍵の件", from_addr="taro@abc.co.jp", status="replied")
    run_sql(tmp_path, "UPDATE mails SET replied_at = '2026-10-01T00:00:00+00:00',"
                      " reply_subject = 'Re: 鍵の件', reply_body = '暗証番号は1234です',"
                      " reply_sent_to = 'taro@abc.co.jp', reply_cc = 'boss@cc.example' WHERE id = ?", (a,))
    # Webメールなどから送って取り込んだメール (添付の名前でも探す)
    s1 = run_sql(tmp_path, "INSERT INTO sent_mails (message_id, to_addr, to_key, subject, body,"
                           " sent_at, fetched_at) VALUES ('<s1@x>', '清掃 チーム <team@clean.example>',"
                           " 'team@clean.example', '来月の清掃', '予定表を送ります',"
                           " '2026-10-02T00:00:00+00:00', 'x')")
    run_sql(tmp_path, "INSERT INTO sent_mail_attachments (sent_id, filename, created_at)"
                      " VALUES (?, '予定表_11月.xlsx', 'x')", (s1,))
    run_sql(tmp_path, "INSERT INTO sent_mails (message_id, to_addr, to_key, subject, body, sent_at,"
                      " fetched_at) VALUES ('<s2@x>', 'x@y.example', 'x@y.example', 'お礼', 'ありがとう',"
                      " '2026-10-03T00:00:00+00:00', 'x')")

    def keys(q, user=admin, **params):
        r = client.get("/api/mail/sent", headers=auth(user), params={"q": q, **params})
        assert r.status_code == 200, r.text
        return r.json()

    out = keys("暗証番号")
    assert [i["key"] for i in out["items"]] == [f"a{a}"] and out["total"] == 1
    assert "暗証番号は1234" in out["items"][0]["snippet"]
    assert out["search"]["mode"] == mode
    assert [i["key"] for i in keys("boss@cc")["items"]] == [f"a{a}"]
    assert [i["key"] for i in keys("鍵")["items"]] == [f"a{a}"]
    assert [i["key"] for i in keys("予定表 11月")["items"]] == [f"i{s1}"]
    assert [i["key"] for i in keys("清掃 チーム", user=staff)["items"]] == [f"i{s1}"]
    assert keys("存在しない")["total"] == 0
    # 取引先の名前で、宛先がその取引先のメール
    r = client.post("/api/mail/clients", headers=auth(admin),
                    json={"name": "クリーン社", "patterns": ["team@clean.example"]})
    assert r.status_code == 200, r.text
    assert [i["key"] for i in keys("クリーン社")["items"]] == [f"i{s1}"]
    # 探さなければ全部
    r = client.get("/api/mail/sent", headers=auth(admin)).json()
    assert r["total"] == 3 and "search" not in r


# ---------- 索引をいつも合わせる ----------

def test_index_follows_updates_deletes_and_attachments(client, users, tmp_path):
    from server import db, mail
    _, _, admin = users
    m = _add_mail(tmp_path, n=1, subject="最初の件名", body="本文")
    assert subjects(client, admin, "最初の件名") == ["最初の件名"]
    run_sql(tmp_path, "UPDATE mails SET subject = '直した件名' WHERE id = ?", (m,))
    assert subjects(client, admin, "最初の件名") == [] and subjects(client, admin, "直した") == ["直した件名"]
    # 相手を決め直した (フォームのお客様)
    run_sql(tmp_path, "UPDATE mails SET contact_addr = 'okyaku@form.example', contact_name = 'お客 様'"
                      " WHERE id = ?", (m,))
    assert subjects(client, admin, "okyaku@form") == ["直した件名"]
    # 添付を足した・消した
    att = run_sql(tmp_path, "INSERT INTO mail_attachments (mail_id, filename, created_at)"
                            " VALUES (?, '間取り図.pdf', 'x')", (m,))
    assert subjects(client, admin, "間取り図") == ["直した件名"]
    run_sql(tmp_path, "DELETE FROM mail_attachments WHERE id = ?", (att,))
    assert subjects(client, admin, "間取り図") == []
    # 消した (保存日数を過ぎた)
    run_sql(tmp_path, "UPDATE mails SET fetched_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (m,))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.purge_old_mails(conn, 30, tmp_path / "mail_attachments")
        assert conn.execute("SELECT COUNT(*) FROM mail_fts").fetchone()[0] == 0


def test_backfill_in_steps_and_scan_until_ready(client, users, tmp_path, monkeypatch):
    """索引を作っている途中は1通ずつ見て探し、少しずつ入れ終えたら索引で探す."""
    from server import db, mail_search
    _, _, admin = users
    # FTS5 が使えない SQLite で動いていた間に入ったメール (トリガーが無い)
    monkeypatch.setattr(mail_search, "_fts_ok", False)
    for i in range(23):
        _add_mail(tmp_path, n=i + 1, subject=f"件名{i}", body="鍵の受け渡し" if i % 2 else "ほか")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        trig = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
                            " AND name LIKE 'trg_%fts%'").fetchone()[0]
        assert trig == 0
        assert conn.execute("SELECT value FROM settings WHERE key = ?",
                            (mail_search.READY_KEY,)).fetchone() is None
    # FTS5 が使える SQLite に戻った。多いので、開いたときには作らない
    monkeypatch.setattr(mail_search, "_fts_ok", True)
    monkeypatch.setattr(mail_search, "BUILD_NOW_MAX", 5)
    r = search(client, admin, "鍵の受け渡し")
    assert r.headers["X-Search-Mode"] == "scan" and r.headers["X-Search-Total"] == "11"
    # 少しずつ入れる (途中で止まっても、続きから)
    class StopAfter:
        """3回入れたら「やめて」と言う (サーバを止めたとき)."""
        def __init__(self, n):
            self.n = n

        def is_set(self):
            self.n -= 1
            return self.n < 0

    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail_search.build_index(conn, batch=4, stop=StopAfter(3))
        assert not mail_search.index_ready(conn)
        prog = conn.execute("SELECT value FROM settings WHERE key = ?",
                            (mail_search.PROGRESS_KEY,)).fetchone()[0]
        assert prog.startswith("12:23:")
    # 入れている間に届いたメールも探せる (トリガー)
    _add_mail(tmp_path, n=40, subject="途中で届いた", body="鍵の受け渡し")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail_search.build_index(conn, batch=4)
        assert mail_search.index_ready(conn)
        assert conn.execute("SELECT COUNT(*) FROM mail_fts").fetchone()[0] == 24
    r = search(client, admin, "鍵の受け渡し")
    assert r.headers["X-Search-Mode"] == "index" and r.headers["X-Search-Total"] == "12"
    # 開き直しても何も書かない
    conn = db.connect(tmp_path / "zatsumu.db")
    try:
        assert conn.total_changes == 0
    finally:
        conn.close()


def test_backfill_retries_when_another_writer_holds_the_db(client, users, tmp_path, monkeypatch):
    """ほかの書き込み (何万通もの削除など) が長く続いて書けなかったときは、待って続きから入れる
    (次の起動まで1通ずつ見て探すことにならない)。ほかの失敗・止めるときはやめる."""
    import sqlite3
    from server import app as app_module, db, mail_search
    for i in range(6):
        _add_mail(tmp_path, n=i + 1, subject=f"鍵 {i}")
    run_sql(tmp_path, "DELETE FROM settings WHERE key = ?", (mail_search.READY_KEY,))
    real = mail_search.build_index
    calls = []

    def flaky(conn, **kw):
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, **kw)

    monkeypatch.setattr(mail_search, "build_index", flaky)
    monkeypatch.setattr(app_module, "SEARCH_BUILD_RETRY_SEC", 0.01)
    app_module._search_build_stop.clear()
    app_module._build_search_index()
    assert len(calls) == 3
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail_search.index_ready(conn)
    # ほかの失敗では、何度も試さない
    run_sql(tmp_path, "DELETE FROM settings WHERE key = ?", (mail_search.READY_KEY,))
    calls.clear()

    def broken(conn, **kw):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: x")

    monkeypatch.setattr(mail_search, "build_index", broken)
    app_module._build_search_index()
    assert len(calls) == 1
    # サーバを止めるときは、待たずにやめる
    calls.clear()

    def locked(conn, **kw):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mail_search, "build_index", locked)
    monkeypatch.setattr(app_module, "SEARCH_BUILD_RETRY_SEC", 30.0)
    app_module._search_build_stop.set()
    started = time.monotonic()
    app_module._build_search_index()
    assert len(calls) == 1 and time.monotonic() - started < 5
    app_module._search_build_stop.clear()


def test_purge_deletes_in_small_steps(client, users, tmp_path, monkeypatch):
    """古いメールは区切って消す (検索の索引も消すので、何万通を1回で消すとほかの書き込みが待たされる)."""
    from server import db, mail
    monkeypatch.setattr(mail, "PURGE_CHUNK", 2)
    old = "2020-01-01T00:00:00+00:00"
    for i in range(5):
        _add_mail(tmp_path, n=i + 1, subject=f"古い {i}", fetched_at=old)
    keep = _add_mail(tmp_path, n=9, subject="新しい", fetched_at="2099-01-01T00:00:00+00:00")
    for i in range(3):
        run_sql(tmp_path, "INSERT INTO sent_mails (message_id, sent_at, fetched_at, subject)"
                          " VALUES (?, ?, ?, '送った')", (f"<s{i}@x>", old, old))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        commits = []
        conn.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
        assert mail.purge_old_mails(conn, 30) == 5
        conn.set_trace_callback(None)
        assert len(commits) >= 5    # 送信箱 3通 (2回) + 届いたメール 5通 (3回)
        assert [r[0] for r in conn.execute("SELECT id FROM mails")] == [keep]
        assert conn.execute("SELECT COUNT(*) FROM sent_mails").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mail_fts").fetchone()[0] == 1


def test_scan_mode_looks_only_at_newest_mails(client, users, tmp_path, monkeypatch):
    from server import mail_search
    _, _, admin = users
    monkeypatch.setattr(mail_search, "_fts_ok", False)
    monkeypatch.setattr(mail_search, "SCAN_MAX", 3)
    for i in range(5):
        _add_mail(tmp_path, n=i + 1, subject=f"鍵 {i}")
    r = search(client, admin, "鍵")
    assert sorted(m["subject"] for m in r.json()) == ["鍵 2", "鍵 3", "鍵 4"]
    assert r.headers["X-Search-Partial"] == "1"
    out = client.get("/api/mail/sent", headers=auth(admin), params={"q": "鍵"}).json()
    assert out["search"]["mode"] == "scan"


# ---------- 速さ ----------

def test_fast_on_50k_mails(tmp_path):
    from server import db, mail_search, sent_box
    path = tmp_path / "zatsumu.db"
    conn = db.connect(path)
    words = ["清掃", "鍵", "予約", "チェックイン", "請求書", "リネン", "キャンセル", "見積", "物件", "ゲスト"]
    now = "2026-10-01T00:00:00+00:00"
    # 前からあるメール (索引が無かった版で入ったもの) として入れてから、索引を作る
    for t in mail_search._TRIGGERS:
        conn.execute(f"DROP TRIGGER {t}")
    conn.execute("DELETE FROM settings WHERE key = ?", (mail_search.READY_KEY,))
    conn.executemany(
        "INSERT INTO mails (message_id, from_addr, from_name, subject, body, received_at,"
        " fetched_at, status, contact_addr, contact_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'from')",
        [(f"<m{i}@x>", f"u{i % 900}@co{i % 300}.example.jp", f"名前{i % 500}",
          f"{words[i % 10]}の件 {i}",
          "いつもお世話になっております。" + "".join(words[(i * 7 + k) % 10] + "について、" for k in range(40))
          + f"管理番号 X{i:05d}", now, now,
          ("unhandled", "replied", "archived")[i % 3], f"u{i % 900}@co{i % 300}.example.jp")
         for i in range(50000)])
    conn.commit()
    t = time.time()
    mail_search.build_index(conn, batch=5000)
    build = time.time() - t
    assert mail_search.index_ready(conn), build

    def run(q, **params):
        from server import app as app_module
        s = mail_search.Search(conn, mail_search.parse_query(q))
        w, p = s.inbox_where()
        where = [w]
        if params.get("status"):
            where.append("m.status = ?")
            p = [*p, params["status"]]
        sql = (app_module._MAIL_LIST_SELECT + " WHERE " + " AND ".join(where)
               + app_module.MAIL_SORTS["date"] + " LIMIT 500")
        t = time.time()
        rows = conn.execute(sql, p).fetchall()
        n = conn.execute("SELECT COUNT(*) FROM mails m WHERE " + " AND ".join(where), p).fetchone()[0]
        return time.time() - t, len(rows), n

    took, shown, n = run("X01234")
    assert (shown, n) == (1, 1) and took < 0.5, took
    took, shown, n = run("チェックイン X0")
    assert n > 0 and took < 2, took
    took, shown, n = run("鍵 清掃", status="unhandled")      # 2文字・1文字だけ (1通ずつ見る)
    assert shown == 500 and took < 4, took
    took, shown, n = run("名前12 鍵")
    assert n > 0 and took < 2, took
    # 索引を使っている (3文字以上の言葉)
    s = mail_search.Search(conn, ["X01234"])
    w, p = s.inbox_where()
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT m.id FROM mails m WHERE " + w, p))
    assert "mail_fts VIRTUAL TABLE INDEX" in plan, plan
    # 送信箱
    t = time.time()
    out = sent_box.list_sent(conn, 0, 50, None, ["X0123"])
    assert out["total"] == 0 and time.time() - t < 2
    conn.close()


# ---------- 前の版 (自動更新で戻ったとき) ----------

def _old_code_dir(tmp_path, rev=PREVIOUS):
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", rev + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip("前の版のコミットがありません (浅い clone)")
    out = tmp_path / "old_code"
    out.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", rev, "server"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(out)], input=archive, check=True)
    return out


def test_previous_version_runs_on_new_db_and_its_mails_are_searchable(tmp_path, monkeypatch):
    old_dir = _old_code_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(data))
    import importlib
    from server import app as app_module, db
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    with db.get_db(data / "zatsumu.db") as conn:
        admin = db.create_user(conn, "黒須", is_admin=True)
        db.set_setting(conn, "smtp_host", "smtp.example.com")
        db.set_setting(conn, "mail_auto_draft", 0)
    r = c.post("/api/mail/accounts", headers=auth(admin), json={
        "from_addr": "info@example.com", "imap_host": "imap.example.com",
        "imap_user": "info@example.com", "imap_pass": "pw", "label": "info"})
    assert r.status_code == 200, r.text
    m = _add_mail(data, n=1, subject="新しい版で入れた鍵の件")
    run_sql(data, "UPDATE mails SET account_id = ? WHERE id = ?", (r.json()["id"], m))
    assert c.get("/api/mail", headers=auth(admin), params={"q": "鍵"}).status_code == 200
    env = {**os.environ, "ZATSUMU_DATA_DIR": str(data)}
    env.pop("ZATSUMU_DEMO", None)
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(old_dir)!r})
        from email.message import EmailMessage
        from fastapi.testclient import TestClient
        from server import app as app_module, mail, notify
        assert app_module.__file__.startswith({str(old_dir)!r})
        notify.smtp_send = lambda s, sender, rcpts, raw: {{}}
        class FakeIMAP:
            def select(self, folder, readonly=False): return ("OK", [b"1"])
            def response(self, key): return ("UIDVALIDITY", [b"1"])
            def uid(self, cmd, *args):
                if cmd == "search": return ("OK", [b"1"])
                m = EmailMessage()
                m["From"] = "Oyama <oyama@guest.example>"
                m["To"] = "info@example.com"
                m["Subject"] = "前の版で受信したリネンの件"
                m["Message-ID"] = "<oldfetch@x>"
                m.set_content("シーツの交換をお願いします")
                m.add_attachment(b"%PDF", maintype="application", subtype="pdf",
                                 filename="交換表.pdf")
                return ("OK", [(b"h", m.as_bytes())])
            def list(self, *a): return ("OK", [b'(\\\\Sent) "/" "Sent"'])
            def append(self, *a): return ("OK", [b""])
            def logout(self): pass
        mail._imap_connect = lambda s: FakeIMAP()
        c = TestClient(app_module.app)
        h = {{"Authorization": "Bearer {admin['token']}"}}
        f = c.post("/api/mail/fetch", headers=h)
        rows = c.get("/api/mail?status=", headers=h).json()
        s = c.post("/api/mail/{m}/send", headers=h, json={{"body": "暗証番号をお送りします"}})
        c.patch("/api/mail/{m}", headers=h, json={{"category": "その他"}})
        print(json.dumps({{"fetch": f.status_code, "count": len(rows), "send": s.status_code}}))
    """)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(old_dir), env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"fetch": 200, "count": 2, "send": 200}
    # 新しい版に戻ると、前の版が受信したメール・送った返信も探せる (DB のトリガーが入れていた)
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert subjects(c, admin, "リネン") == ["前の版で受信したリネンの件"]
    assert subjects(c, admin, "シーツ 交換") == ["前の版で受信したリネンの件"]
    assert subjects(c, admin, "交換表") == ["前の版で受信したリネンの件"]
    assert subjects(c, admin, "oyama") == ["前の版で受信したリネンの件"]
    r = c.get("/api/mail/sent", headers=auth(admin), params={"q": "暗証番号"}).json()
    assert r["total"] == 1 and r["search"]["mode"] == "index"


# ---------- 画面 ----------

def test_mail_page_has_search_boxes(client):
    page = client.get("/mail").text
    for marker in ('id="inbox-q"', 'id="inbox-q-clear"', 'id="inbox-q-info"', 'id="sent-q"',
                   'id="sent-q-clear"', 'maxlength="100"', "X-Search-Total", 'q.set("q", word)',
                   'p.set("q", word)', "compositionend", "syncSearchStatus", "function hl("):
        assert marker in page, marker
    # 印は、外から来た文字を esc() してから付ける
    assert "out += esc(str.slice(last, m.index)) + '<mark class=\"hit\">' + esc(m[0])" in page
    for safe in ("${hl(m.subject, T)", "${hl(contactLabel(m), T)}", "${hl(it.subject, T)",
                 "hl(m.snippet, T)", "${esc(inboxFound.q)}", "${esc(q)}"):
        assert safe in page, safe
    for unsafe in ("${m.snippet}", "${inboxFound.q}", "${it.snippet}"):
        assert unsafe not in page, unsafe
    # iPhone で欄を押しても拡大されない文字の大きさ
    assert "font-size: max(16px, 1rem)" in page
