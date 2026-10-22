"""複数のメールアドレス (kurosu@・info@ など) をまとめて受信するテスト.

- 旧設定 (受信サーバが1つだけ) からの移し替え (受信の続きの位置を引き継ぎ、読み直さない)
- アドレスごとの受信 (1つが失敗しても、ほかは受信する)・続きの位置・受信の様子
- 返信の差出人と送信のログイン (アドレスごと / 共通)・差出人を断られたときの説明
- 送った返信の控えを、届いたアドレスのメールボックスに残す
- 自分たちのアドレス (控えを取り込まない・全員に返信の CC に入れない)
- アドレスの追加・変更・削除 (メールは残す)・パスワードを返さない

ネットワークには出ない (IMAP・SMTP は偽物に差し替える)。
"""
import email
import email.policy
import imaplib
import smtplib
import sqlite3
from email.message import EmailMessage

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
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
    return worker, admin


def raw_mail(n, frm=None, to="info@bics.example"):
    msg = EmailMessage()
    msg["From"] = frm or f"guest{n}@example.com"
    msg["To"] = to
    msg["Subject"] = f"件名{n}"
    msg["Message-ID"] = f"<u{n}@example.com>"
    msg.set_content("本文")
    return msg.as_bytes()


class Box:
    """1つのメールボックス (UID → メール)。search の条件を覚える."""

    def __init__(self, mails, uidvalidity=b"111", fail=None):
        self.mails, self.uidvalidity, self.fail = dict(mails), uidvalidity, fail
        self.criteria = []
        self.on_search = None

    def select(self, folder, readonly=False):
        assert readonly   # 受信箱は変更しない
        return ("OK", [str(len(self.mails)).encode()])

    def response(self, key):
        return ("UIDVALIDITY", [self.uidvalidity])

    def uid(self, cmd, *args):
        if cmd == "search":
            self.criteria.append(args[1])
            if self.on_search:
                self.on_search()
            return ("OK", [b" ".join(str(u).encode() for u in sorted(self.mails))])
        return ("OK", [(b"header", self.mails[int(args[0])])])

    def logout(self):
        pass


def use_boxes(monkeypatch, boxes):
    """受信サーバの名前 → Box で IMAP をつなぐ (fail があればログインで失敗する)."""
    from server import mail
    logins = []

    def connect(s):
        logins.append((s["imap_host"], s["imap_user"], s["imap_pass"]))
        box = boxes[s["imap_host"]]
        if box.fail:
            raise box.fail
        return box

    monkeypatch.setattr(mail, "_imap_connect", connect)
    return logins


ACCOUNT = {"from_addr": "info@bics.example", "imap_host": "imap.bics.example",
           "imap_user": "info@bics.example", "imap_pass": "info-secret-pw"}
KUROSU = {"from_addr": "kurosu@bics.example", "imap_host": "imap2.bics.example",
          "imap_user": "kurosu@bics.example", "imap_pass": "kurosu-secret-pw",
          "label": "黒須"}


def add_account(client, admin, **fields):
    r = client.post("/api/mail/accounts", headers=auth(admin), json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def accounts_rows(tmp_path):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mail_accounts ORDER BY id")]


def mails_rows(tmp_path):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, message_id, account_id, from_addr FROM mails ORDER BY id")]


def status(client, admin):
    r = client.get("/api/mail/status", headers=auth(admin))
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 旧設定からの移し替え ----------

def _legacy_db(path, **settings):
    """この機能より前の DB (受信サーバの設定が1つだけ・受信の続きの位置あり・メールあり)."""
    from server import db
    conn = db.connect(path)
    conn.execute("DROP TABLE mail_accounts")
    conn.execute("DELETE FROM settings WHERE key = ?", (db.MAIL_ACCOUNTS_MIGRATED_KEY,))
    base = {"imap_host": "imap.bics.example", "imap_port": "993", "imap_use_ssl": 1,
            "imap_user": "info@bics.example", "imap_pass": "legacy-secret-pw",
            "imap_folder": "INBOX", "imap_sent_folder": "INBOX.Sent", "mail_save_sent": 1,
            "mail_from": "株式会社ビックス <info@bics.example>", "smtp_user": "info@bics.example",
            "mail_state_uidvalidity": "777", "mail_state_last_uid": "120",
            "mail_state_fetch_ok_at": "2026-10-20T00:00:00+00:00",
            **settings}
    for key, value in base.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     (key, str(value)))
    conn.commit()
    conn.close()
    # mails.account_id が無かった頃の形にする (列を消すため、作り直す)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.executescript("""
        CREATE TABLE mails_old AS SELECT * FROM mails;
        DROP TABLE mails;
        CREATE TABLE mails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE, imap_uid INTEGER,
            from_addr TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '',
            to_addr TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
            priority_reason TEXT NOT NULL DEFAULT '', priority_source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unhandled', draft_reply TEXT NOT NULL DEFAULT '',
            draft_source TEXT NOT NULL DEFAULT '', draft_generated_at TEXT,
            reply_subject TEXT NOT NULL DEFAULT '', reply_body TEXT NOT NULL DEFAULT '',
            replied_at TEXT, replied_by TEXT NOT NULL DEFAULT '',
            references_hdr TEXT NOT NULL DEFAULT '');
        DROP TABLE mails_old;
    """)
    for n in (1, 2):
        raw.execute("INSERT INTO mails (message_id, from_addr, received_at, fetched_at)"
                    " VALUES (?, 'old@example.com', '2026-10-01T00:00:00+00:00',"
                    " '2026-10-01T00:00:00+00:00')", (f"<old{n}@example.com>",))
    raw.commit()
    raw.close()


def test_legacy_settings_become_the_first_account(tmp_path):
    from server import db
    path = tmp_path / "old.db"
    _legacy_db(path)
    for _ in range(2):   # 何度開いても、1つだけ作る
        conn = db.connect(path)
        accs = [dict(r) for r in conn.execute("SELECT * FROM mail_accounts")]
        assert len(accs) == 1
        a = accs[0]
        assert (a["imap_host"], a["imap_user"], a["imap_pass"]) == (
            "imap.bics.example", "info@bics.example", "legacy-secret-pw")   # パスワードも移す
        assert (a["imap_port"], a["imap_ssl"], a["imap_folder"], a["imap_sent_folder"],
                a["save_sent"]) == ("993", 1, "INBOX", "INBOX.Sent", 1)
        assert a["from_addr"] == "株式会社ビックス <info@bics.example>"
        assert a["label"] == "info" and a["enabled"] == 1
        assert a["smtp_user"] == "" and a["smtp_pass"] == ""   # 送信は共通の設定のまま
        # 受信の続きの位置と受信の様子を引き継ぐ
        assert (a["state_uidvalidity"], a["state_last_uid"]) == ("777", 120)
        assert a["fetch_ok_at"] == "2026-10-20T00:00:00+00:00"
        # 前からあるメールはこのアドレスに届いたことにする
        assert [r[0] for r in conn.execute("SELECT account_id FROM mails")] == [a["id"]] * 2
        # 旧設定のキーは消さない (前の版に戻ったときも受信できるように)
        s = db.get_settings(conn)
        assert s["imap_host"] == "imap.bics.example" and s["imap_pass"] == "legacy-secret-pw"
        assert conn.execute("SELECT value FROM settings WHERE key = 'mail_state_last_uid'"
                            ).fetchone()[0] == "120"
        conn.close()


def test_migrated_account_continues_without_refetching(tmp_path, monkeypatch):
    """引き継いだ続きの位置から受信する (取り込み済みのメールを読み直さない・取り逃さない)."""
    from server import db, mail
    path = tmp_path / "old.db"
    _legacy_db(path, mail_auto_draft=0)
    box = Box({119: raw_mail(119), 120: raw_mail(120), 121: raw_mail(121)},
              uidvalidity=b"777")
    use_boxes(monkeypatch, {"imap.bics.example": box})
    conn = db.connect(path)
    added = mail.fetch_mail_locked(conn, db.get_settings(conn))
    assert box.criteria == ["(UID 121:*)"]
    assert [a["message_id"] for a in added] == ["<u121@example.com>"]
    acc = conn.execute("SELECT * FROM mail_accounts").fetchone()
    assert acc["state_last_uid"] == 121
    assert conn.execute("SELECT account_id FROM mails WHERE message_id = '<u121@example.com>'"
                        ).fetchone()[0] == acc["id"]
    conn.close()


def test_no_account_without_legacy_imap_and_none_after_deleting_all(client, users, tmp_path):
    """受信をまだ使っていないなら作らない。アドレスを全部消した後に、旧設定から作り直さない."""
    from server import db
    _, admin = users
    assert accounts_rows(tmp_path) == []
    a = add_account(client, admin, **ACCOUNT)
    client.patch("/api/settings", headers=auth(admin), json={"imap_host": "imap.old.example"})
    assert client.delete(f"/api/mail/accounts/{a['id']}", headers=auth(admin)).status_code == 200
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert db.get_settings(conn)["imap_host"] == "imap.old.example"
    assert accounts_rows(tmp_path) == []
    assert status(client, admin)["imap_set"] is False


def test_fill_missing_accounts_after_running_the_previous_version(client, users, tmp_path):
    """前の版に戻っていた間に取り込んだメール (アドレスなし) は、旧設定から作ったアドレスにする.

    アドレスを削除して「削除したアドレス」になったメールは変えない。
    """
    from server import db, mail
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_host": "imap.bics.example", "imap_user": "info@bics.example",
                       "imap_pass": "pw"})
    first = _add_mail(tmp_path, n=1)          # この接続で旧設定から1つ目のアドレスを作る
    legacy = accounts_rows(tmp_path)[0]["id"]
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fill_missing_accounts(conn) == 1   # 作った後に入った分 (前の版と同じ)
    other = add_account(client, admin, **KUROSU)
    deleted_mail = _add_mail(tmp_path, n=2)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.fill_missing_accounts(conn)
        conn.execute("UPDATE mails SET account_id = ? WHERE id = ?", (other["id"], deleted_mail))
    client.delete(f"/api/mail/accounts/{other['id']}", headers=auth(admin))
    during_old_version = _add_mail(tmp_path, n=3)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.fill_missing_accounts(conn) == 1
        got = dict(conn.execute("SELECT id, account_id FROM mails").fetchall())
        assert mail.fill_missing_accounts(conn) == 0   # 2回目は何もしない
    assert got == {first: legacy, deleted_mail: None, during_old_version: legacy}


# ---------- アドレスごとの受信 ----------

def test_fetch_each_account_and_one_failure_does_not_block(client, users, tmp_path,
                                                           monkeypatch):
    from server import db
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"mail_auto_draft": False})
    info = add_account(client, admin, **ACCOUNT)
    kurosu = add_account(client, admin, **KUROSU)
    boxes = {"imap.bics.example": Box({5: raw_mail(5)}),
             "imap2.bics.example": Box({9: raw_mail(9, to="kurosu@bics.example")},
                                       fail=imaplib.IMAP4.error(
                                           "[AUTHENTICATIONFAILED] bad kurosu-secret-pw"))}
    logins = use_boxes(monkeypatch, boxes)
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched"] == 1
    assert body["errors"][0]["label"] == "黒須"
    assert "ログインできませんでした" in body["errors"][0]["error"]
    assert "kurosu-secret-pw" not in r.text                      # パスワードは伏せる
    # それぞれのアドレスのユーザー・パスワードでログインする
    assert logins == [("imap.bics.example", "info@bics.example", "info-secret-pw"),
                      ("imap2.bics.example", "kurosu@bics.example", "kurosu-secret-pw")]
    assert [(m["message_id"], m["account_id"]) for m in mails_rows(tmp_path)] == [
        ("<u5@example.com>", info["id"])]
    s = status(client, admin)
    assert s["last_error_account"] == "黒須" and s["error_count"] == 1
    assert "ログインできませんでした" in s["last_error"]
    by_id = {a["id"]: a for a in s["accounts"]}
    assert by_id[info["id"]]["last_error"] == "" and by_id[info["id"]]["last_fetch_at"]
    assert by_id[kurosu["id"]]["last_fetch_at"] is None
    # 直ったら、そのアドレスも受信して失敗の記録は消える
    boxes["imap2.bics.example"].fail = None
    assert client.post("/api/mail/fetch", headers=auth(admin)).json() == {
        "fetched": 1, "errors": []}
    assert [(m["message_id"], m["account_id"]) for m in mails_rows(tmp_path)][-1] == (
        "<u9@example.com>", kurosu["id"])
    s = status(client, admin)
    assert s["last_error"] == "" and s["error_count"] == 0
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert conn.execute("SELECT fetch_error FROM mail_accounts WHERE id = ?",
                            (kurosu["id"],)).fetchone()[0] == ""


def test_all_accounts_failing_is_an_error_with_each_reason(client, users, monkeypatch):
    _, admin = users
    add_account(client, admin, **ACCOUNT)
    add_account(client, admin, **KUROSU)
    use_boxes(monkeypatch, {
        "imap.bics.example": Box({}, fail=TimeoutError("timed out")),
        "imap2.bics.example": Box({}, fail=ConnectionRefusedError(111, "refused"))})
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "info: 受信サーバから時間内に返事がありませんでした" in detail
    assert "黒須: 受信サーバにつながりませんでした" in detail


def test_background_fetch_raises_only_when_every_account_fails(client, users, tmp_path,
                                                                monkeypatch):
    """定期受信は1つでも受信できれば、優先度[高]の通知もする (失敗はログと画面に出す)."""
    from server import db, mail
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"mail_auto_draft": False, "mail_urgent_keywords": "件名"})
    add_account(client, admin, **ACCOUNT)
    add_account(client, admin, **KUROSU)
    boxes = {"imap.bics.example": Box({1: raw_mail(1)}),
             "imap2.bics.example": Box({}, fail=OSError(101, "unreachable"))}
    use_boxes(monkeypatch, boxes)
    sent = []
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.check_mail(conn, notifier=lambda s, text: sent.append(text)) == 1
    assert len(sent) == 1 and "件名1" in sent[0]
    boxes["imap.bics.example"].fail = OSError(101, "unreachable")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        with pytest.raises(OSError):
            mail.check_mail(conn)


def test_disabled_account_is_not_fetched(client, users, tmp_path, monkeypatch):
    from server import db, mail
    _, admin = users
    a = add_account(client, admin, **ACCOUNT)
    logins = use_boxes(monkeypatch, {"imap.bics.example": Box({})})
    r = client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin),
                     json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.check_mail(conn) == 0
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 400 and "止めています" in r.json()["detail"]
    s = status(client, admin)
    assert s["imap_set"] is False and s["auto_fetch"] is False
    assert logins == []


def test_uid_state_is_kept_per_account(client, users, tmp_path, monkeypatch):
    from server import db
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"mail_auto_draft": False})
    info = add_account(client, admin, **ACCOUNT)
    kurosu = add_account(client, admin, **KUROSU)
    # 2つのメールボックスで UID が重なっても、それぞれの続きから読む
    boxes = {"imap.bics.example": Box({10: raw_mail(10), 11: raw_mail(11)}, b"1"),
             "imap2.bics.example": Box({3: raw_mail(3)}, b"1")}
    use_boxes(monkeypatch, boxes)
    client.post("/api/mail/fetch", headers=auth(admin))
    rows = {a["id"]: a for a in accounts_rows(tmp_path)}
    assert (rows[info["id"]]["state_uidvalidity"], rows[info["id"]]["state_last_uid"]) == ("1", 11)
    assert (rows[kurosu["id"]]["state_uidvalidity"], rows[kurosu["id"]]["state_last_uid"]) == ("1", 3)
    boxes["imap2.bics.example"].mails[4] = raw_mail(4)
    client.post("/api/mail/fetch", headers=auth(admin))
    assert boxes["imap.bics.example"].criteria[-1] == "(UID 12:*)"
    assert boxes["imap2.bics.example"].criteria[-1] == "(UID 4:*)"
    assert len(mails_rows(tmp_path)) == 4
    # 旧設定の続きの位置 (前の版のもの) は使わない・変えない
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'mail_state_last_uid'"
                            ).fetchone() is None
    # 接続先を変えたら続きの位置を消す (新しい接続先は初回と同じく日数で遡る)
    r = client.patch(f"/api/mail/accounts/{kurosu['id']}", headers=auth(admin),
                     json={"imap_folder": "INBOX.kurosu"})
    assert r.status_code == 200
    row = [a for a in accounts_rows(tmp_path) if a["id"] == kurosu["id"]][0]
    assert (row["state_uidvalidity"], row["state_last_uid"]) == ("", 0)
    client.post("/api/mail/fetch", headers=auth(admin))
    assert boxes["imap2.bics.example"].criteria[-1].startswith("(SINCE ")
    assert len(mails_rows(tmp_path)) == 4   # 取り込み済みのメールは二重に入らない


def test_state_is_not_written_when_connection_changed_during_fetch(client, users, tmp_path,
                                                                   monkeypatch):
    """受信している間に接続先を変えたら、前の接続先の続きの位置を書かない."""
    from server import db
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"mail_auto_draft": False})
    a = add_account(client, admin, **ACCOUNT)
    box = Box({50: raw_mail(50)})

    def change_folder():
        with db.get_db(tmp_path / "zatsumu.db") as other:
            other.execute("UPDATE mail_accounts SET imap_folder = 'Other',"
                          " state_uidvalidity = '', state_last_uid = 0 WHERE id = ?", (a["id"],))

    box.on_search = change_folder
    use_boxes(monkeypatch, {"imap.bics.example": box})
    assert client.post("/api/mail/fetch", headers=auth(admin)).json()["fetched"] == 1
    row = accounts_rows(tmp_path)[0]
    assert (row["imap_folder"], row["state_last_uid"]) == ("Other", 0)


def test_status_shows_the_longest_failing_account(client, users, tmp_path):
    from server import db, mail
    _, admin = users
    a = add_account(client, admin, **ACCOUNT)
    b = add_account(client, admin, **KUROSU)
    c = add_account(client, admin, label="止めた", from_addr="off@bics.example",
                    imap_host="imap.bics.example", imap_user="off@bics.example",
                    imap_pass="x", enabled=False)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mail_accounts SET fetch_ok_at = ?, fetch_error = 'A の理由',"
                     " fetch_error_at = ? WHERE id = ?",
                     ("2026-10-20T00:00:00+00:00", "2026-10-22T01:00:00+00:00", a["id"]))
        conn.execute("UPDATE mail_accounts SET fetch_ok_at = ?, fetch_error = 'B の理由',"
                     " fetch_error_at = ? WHERE id = ?",
                     ("2026-10-21T00:00:00+00:00", "2026-10-22T02:00:00+00:00", b["id"]))
        # 止めているアドレスの失敗は数えない
        conn.execute("UPDATE mail_accounts SET fetch_error = 'C の理由' WHERE id = ?", (c["id"],))
        state = mail.fetch_state(conn)
    assert state["last_error"] == "A の理由" and state["last_error_account"] == "info"
    assert state["last_error_at"] == "2026-10-22T01:00:00+00:00"
    assert state["error_count"] == 2
    assert state["last_fetch_at"] == "2026-10-20T00:00:00+00:00"   # 一番前の最終受信
    assert [x["label"] for x in state["accounts"]] == ["info", "黒須", "止めた"]


# ---------- 返信の差出人と送信のログイン ----------

def _capture_smtp(monkeypatch):
    from server import notify
    sent = []
    monkeypatch.setattr(notify, "smtp_send", lambda s, sender, rcpts, message: sent.append(
        {"user": s.get("smtp_user"), "pass": s.get("smtp_pass"), "sender": sender,
         "rcpts": rcpts, "msg": message}) or {})
    return sent


def _setup_send(client, admin, monkeypatch):
    from server import mail
    client.patch("/api/settings", headers=auth(admin), json={
        "smtp_host": "smtp.bics.example", "smtp_user": "info@bics.example",
        "smtp_pass": "shared-smtp-pw", "mail_from": "info@bics.example"})
    saved = []
    monkeypatch.setattr(mail, "save_to_sent",
                        lambda s, raw: saved.append((s["imap_host"], s["imap_user"],
                                                     s["imap_pass"], s["imap_sent_folder"]))
                        or "Sent")
    return saved


def _mail_for(tmp_path, account_id, n=1):
    from server import db
    mid = _add_mail(tmp_path, n=n)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET account_id = ? WHERE id = ?", (account_id, mid))
    return mid


def test_reply_is_sent_from_the_address_it_arrived_at(client, users, tmp_path, monkeypatch):
    _, admin = users
    saved = _setup_send(client, admin, monkeypatch)
    kurosu = add_account(client, admin, **{**KUROSU, "from_addr": "黒須 太郎 <kurosu@bics.example>",
                                          "imap_sent_folder": "INBOX.Sent"})
    sent = _capture_smtp(monkeypatch)
    mid = _mail_for(tmp_path, kurosu["id"])
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["account_label"] == "黒須" and d["account_address"] == "kurosu@bics.example"
    assert d["reply_from"] == "黒須 太郎 <kurosu@bics.example>"
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 200, r.text
    assert r.json()["from"] == "黒須 太郎 <kurosu@bics.example>"
    # 送信のログインは共通の設定 (このアドレスに送信用のユーザーが無いので)。差出人は kurosu@
    assert (sent[0]["user"], sent[0]["pass"]) == ("info@bics.example", "shared-smtp-pw")
    assert sent[0]["sender"] == "kurosu@bics.example"
    msg = email.message_from_string(sent[0]["msg"], policy=email.policy.default)
    assert msg["From"].addresses[0].addr_spec == "kurosu@bics.example"
    assert msg["From"].addresses[0].display_name == "黒須 太郎"   # 日本語の名前も壊れない
    assert msg["Message-ID"].endswith("@bics.example>")
    # 控えは kurosu@ のメールボックスに残す
    assert saved == [("imap2.bics.example", "kurosu@bics.example", "kurosu-secret-pw",
                      "INBOX.Sent")]
    import datetime as _dt
    csv_text = client.get("/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
                          headers=auth(admin)).text
    assert "from=kurosu@bics.example" in csv_text


def test_reply_uses_the_accounts_own_smtp_login(client, users, tmp_path, monkeypatch):
    _, admin = users
    _setup_send(client, admin, monkeypatch)
    kurosu = add_account(client, admin, **KUROSU, smtp_user="kurosu@bics.example",
                         smtp_pass="kurosu-smtp-pw", save_sent=False)
    assert kurosu["smtp_pass_set"] is True and "kurosu-smtp-pw" not in str(kurosu)
    sent = _capture_smtp(monkeypatch)
    from server import mail

    def must_not_save(s, raw):
        raise AssertionError("控えを残さない設定なのに呼ばれた")

    monkeypatch.setattr(mail, "save_to_sent", must_not_save)
    mid = _mail_for(tmp_path, kurosu["id"])
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 200, r.text
    assert (sent[0]["user"], sent[0]["pass"]) == ("kurosu@bics.example", "kurosu-smtp-pw")
    assert r.json()["saved_to_sent"] is False


def test_sender_refused_by_shared_smtp_is_explained(client, users, tmp_path, monkeypatch):
    from server import notify
    _, admin = users
    _setup_send(client, admin, monkeypatch)
    kurosu = add_account(client, admin, **KUROSU)

    def refuse(s, sender, rcpts, message):
        raise smtplib.SMTPSenderRefused(
            553, b"5.7.1 <kurosu@bics.example>: Sender address rejected: not owned by user "
                 b"info@bics.example (shared-smtp-pw)", sender)

    monkeypatch.setattr(notify, "smtp_send", refuse)
    mid = _mail_for(tmp_path, kurosu["id"])
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "差出人のアドレス（kurosu@bics.example）を断られました" in detail
    assert "共通の送信の設定（SMTPユーザー: info@bics.example）" in detail
    assert "「黒須」" in detail and "送信用のユーザー" in detail
    assert "shared-smtp-pw" not in detail
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "unhandled"   # 送れていないので未対応に戻る
    # このアドレスの送信用のユーザーでも断られたときは、そのユーザーを確かめるよう案内する
    client.patch(f"/api/mail/accounts/{kurosu['id']}", headers=auth(admin),
                 json={"smtp_user": "kurosu@bics.example", "smtp_pass": "kurosu-smtp-pw"})
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 502
    assert "送信用のユーザーで、このアドレスを差出人にして送れるか" in r.json()["detail"]


@pytest.mark.parametrize("exc, expected", [
    (smtplib.SMTPSenderRefused(553, b"rejected", "a@b.example"), True),
    # MAIL FROM への一時的な失敗・容量オーバーは差出人の問題ではない
    (smtplib.SMTPSenderRefused(451, b"4.7.1 Sender rate limit exceeded", "a@b.example"), False),
    (smtplib.SMTPSenderRefused(421, b"4.4.2 Connection dropped", "a@b.example"), False),
    (smtplib.SMTPSenderRefused(552, b"5.3.4 Message size exceeds fixed limit", "a@b.example"),
     False),
    (smtplib.SMTPRecipientsRefused({"x@y.example": (553, b"5.7.1 Sender address rejected: "
                                                          b"not owned by user")}), True),
    (smtplib.SMTPDataError(554, b"5.2.252 SendAsDenied; info@ not allowed to send as kurosu@"),
     True),
    (smtplib.SMTPRecipientsRefused({"x@y.example": (550, b"5.1.1 User unknown")}), False),
    (smtplib.SMTPAuthenticationError(535, b"Authentication credentials invalid"), False),
    (OSError("network"), False),
])
def test_sender_refused_detection(exc, expected):
    from server import mail
    assert mail.sender_refused(exc) is expected


def test_reply_to_mail_of_deleted_address_uses_shared_sender(client, users, tmp_path,
                                                             monkeypatch):
    """削除したアドレスに届いたメールは、共通の差出人で送る (控えは同じアドレスがあればそこに)."""
    _, admin = users
    saved = _setup_send(client, admin, monkeypatch)
    info = add_account(client, admin, **ACCOUNT)
    kurosu = add_account(client, admin, **KUROSU)
    mid = _mail_for(tmp_path, kurosu["id"])
    assert client.delete(f"/api/mail/accounts/{kurosu['id']}",
                         headers=auth(admin)).json() == {"deleted": kurosu["id"], "mails_kept": 1}
    sent = _capture_smtp(monkeypatch)
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["account_id"] is None and d["account_label"] == ""
    assert d["reply_from"] == "info@bics.example"
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 200, r.text
    assert sent[0]["sender"] == "info@bics.example"
    # 共通の差出人 (info@) のメールボックスに控えを残す
    assert saved == [("imap.bics.example", "info@bics.example", "info-secret-pw", "")]
    # 共通の差出人と同じアドレスが無ければ、控えは残さない
    client.delete(f"/api/mail/accounts/{info['id']}", headers=auth(admin))
    mid2 = _add_mail(tmp_path, n=2)
    assert client.post(f"/api/mail/{mid2}/send", headers=auth(admin),
                       json={"body": "本文"}).json()["saved_to_sent"] is False
    assert len(saved) == 1


# ---------- 自分たちのアドレス ----------

def test_own_copies_from_any_account_are_not_fetched(client, users, tmp_path, monkeypatch):
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"mail_auto_draft": False})
    add_account(client, admin, **ACCOUNT)
    add_account(client, admin, **{**KUROSU, "from_addr": "黒須 <Kurosu@bics.example>"})
    add_account(client, admin, label="営業", from_addr="sales@bics.example",
                imap_host="imap3.bics.example", imap_user="sales-login", imap_pass="x",
                smtp_user="sales-sender@bics.example", smtp_pass="y")
    use_boxes(monkeypatch, {
        "imap.bics.example": Box({1: raw_mail(1, frm="kurosu@bics.example"),
                                  2: raw_mail(2, frm="sales-sender@bics.example"),
                                  3: raw_mail(3)}),
        "imap2.bics.example": Box({}), "imap3.bics.example": Box({})})
    assert client.post("/api/mail/fetch", headers=auth(admin)).json()["fetched"] == 1
    assert [m["message_id"] for m in mails_rows(tmp_path)] == ["<u3@example.com>"]


def test_reply_all_excludes_every_account_address(client, users, tmp_path):
    from server import db
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"mail_from": "info@bics.example"})
    add_account(client, admin, **KUROSU)
    add_account(client, admin, label="営業", from_addr="sales@bics.example",
                imap_host="imap3.bics.example", imap_user="sales-in@bics.example",
                imap_pass="x")
    mid = _add_mail(tmp_path)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET to_addr = ?, cc_addr = ? WHERE id = ?",
                     ("info@bics.example, KUROSU@bics.example",
                      "sales@bics.example, sales-in@bics.example, guest@x.example", mid))
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["reply_cc_candidates"] == ["guest@x.example"]


# ---------- 一覧・追加・変更・削除 ----------

def test_list_and_filter_by_account(client, users, tmp_path):
    _, admin = users
    info = add_account(client, admin, **ACCOUNT)
    kurosu = add_account(client, admin, **KUROSU)
    a = _mail_for(tmp_path, info["id"], n=1)
    b = _mail_for(tmp_path, kurosu["id"], n=2)
    rows = client.get("/api/mail", headers=auth(admin)).json()
    assert {r["id"]: r["account_label"] for r in rows} == {a: "info", b: "黒須"}
    got = client.get(f"/api/mail?account={kurosu['id']}", headers=auth(admin)).json()
    assert [r["id"] for r in got] == [b]
    client.delete(f"/api/mail/accounts/{kurosu['id']}", headers=auth(admin))
    got = client.get("/api/mail?account=none", headers=auth(admin)).json()
    assert [(r["id"], r["account_id"], r["account_label"]) for r in got] == [(b, None, "")]
    assert status(client, admin)["deleted_mail_count"] == 1
    assert client.get("/api/mail?account=abc", headers=auth(admin)).status_code == 400


def test_secrets_are_never_returned(client, users, tmp_path):
    _, admin = users
    a = add_account(client, admin, **ACCOUNT, smtp_user="info@bics.example",
                    smtp_pass="smtp-secret-pw")
    texts = [str(a)]
    r = client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin),
                     json={"imap_pass": "new-imap-pw", "label": "インフォ"})
    texts.append(r.text)
    texts.append(client.get("/api/mail/accounts", headers=auth(admin)).text)
    texts.append(client.get("/api/settings", headers=auth(admin)).text)
    texts.append(client.get("/api/mail/status", headers=auth(admin)).text)
    import datetime as _dt
    texts.append(client.get("/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
                            headers=auth(admin)).text)
    for t in texts:
        for secret in ("info-secret-pw", "smtp-secret-pw", "new-imap-pw"):
            assert secret not in t
    listed = client.get("/api/mail/accounts", headers=auth(admin)).json()["accounts"][0]
    assert listed["imap_pass_set"] is True and listed["smtp_pass_set"] is True
    assert "imap_pass" not in listed and "smtp_pass" not in listed
    # パスワードは送らなければ変えない
    client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin), json={"imap_pass": ""})
    assert accounts_rows(tmp_path)[0]["imap_pass"] == "new-imap-pw"
    # 送信用のユーザーを空にしたら、送信用のパスワードも消す
    r = client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin), json={"smtp_user": ""})
    assert r.json()["smtp_pass_set"] is False and accounts_rows(tmp_path)[0]["smtp_pass"] == ""


def test_connection_test_masks_secrets(client, users, monkeypatch):
    from server import mail
    _, admin = users
    a = add_account(client, admin, **ACCOUNT)

    def boom(s):
        assert s["imap_pass"] == "info-secret-pw" and s["imap_host"] == "imap.bics.example"
        raise RuntimeError("login failed for info-secret-pw")

    monkeypatch.setattr(mail, "test_imap", boom)
    r = client.post(f"/api/mail/accounts/{a['id']}/test", headers=auth(admin))
    assert r.status_code == 502 and "info-secret-pw" not in r.text
    monkeypatch.setattr(mail, "test_imap", lambda s: {
        "count": 3, "sent_folder": "INBOX.Sent", "sent_folder_error": ""})
    r = client.post(f"/api/mail/accounts/{a['id']}/test", headers=auth(admin))
    assert r.json() == {"ok": True, "count": 3, "sent_folder": "INBOX.Sent",
                        "sent_folder_error": "", "label": "info"}
    assert client.post("/api/mail/accounts/999/test", headers=auth(admin)).status_code == 404


@pytest.mark.parametrize("fields, words", [
    ({"imap_pass": ""}, "IMAPパスワードを入れてください"),
    ({"imap_host": "imap example.com"}, "IMAPサーバの名前"),
    ({"imap_port": "99999"}, "1〜65535"),
    ({"from_addr": "not-an-address"}, "メールアドレス「not-an-address」が正しくありません"),
    ({"from_addr": "a@b.example\r\nBcc: x@y.example"}, "改行"),
    ({"imap_pass": "pw\r\nA001 LOGOUT"}, "改行"),
    ({"smtp_user": "info@bics.example"}, "送信用のパスワードも入れてください"),
    ({"label": "あ" * 21}, "名前が長すぎます"),
    ({"imap_user": "", "from_addr": ""}, "メールアドレスを入れてください"),
])
def test_account_validation(client, users, fields, words):
    _, admin = users
    r = client.post("/api/mail/accounts", headers=auth(admin), json={**ACCOUNT, **fields})
    assert r.status_code == 400 and words in r.json()["detail"], r.text


def test_account_defaults_and_duplicates(client, users):
    _, admin = users
    # メールアドレスが空なら IMAP ユーザー、名前が空なら @ の前。ポートは SSL なら 993
    a = add_account(client, admin, imap_host="imap.bics.example",
                    imap_user="Info@bics.example", imap_pass="pw")
    assert (a["from_addr"], a["label"], a["imap_port"], a["imap_ssl"], a["imap_folder"],
            a["save_sent"], a["enabled"]) == ("Info@bics.example", "Info", "993", True,
                                              "INBOX", True, True)
    b = add_account(client, admin, imap_host="imap.bics.example", imap_user="sales",
                    imap_pass="pw", imap_ssl=False, from_addr='"Bics, Sales" <sales@bics.example>')
    assert b["imap_port"] == "143" and b["label"] == "sales" and b["address"] == "sales@bics.example"
    # 同じメールボックスは二重に登録しない (大文字・小文字の違いも同じ)
    r = client.post("/api/mail/accounts", headers=auth(admin),
                    json={"imap_host": "IMAP.bics.example", "imap_user": "info@BICS.example",
                          "imap_pass": "pw", "label": "別の名前"})
    assert r.status_code == 409 and "同じメールボックス" in r.json()["detail"]
    r = client.post("/api/mail/accounts", headers=auth(admin), json={**KUROSU, "label": "info"})
    assert r.status_code == 409 and "名前" in r.json()["detail"]
    # 一般のメンバーは使えない
    assert client.get("/api/mail/accounts").status_code == 401


def test_account_limit_and_reorder(client, users, monkeypatch):
    from server import mail
    _, admin = users
    monkeypatch.setattr(mail, "ACCOUNT_MAX_COUNT", 3)
    ids = [add_account(client, admin, imap_host="imap.bics.example",
                       imap_user=f"u{n}@bics.example", imap_pass="pw")["id"] for n in range(3)]
    r = client.post("/api/mail/accounts", headers=auth(admin),
                    json={**KUROSU})
    assert r.status_code == 400 and "3個まで" in r.json()["detail"]
    client.patch(f"/api/mail/accounts/{ids[2]}", headers=auth(admin), json={"move": "up"})
    listed = client.get("/api/mail/accounts", headers=auth(admin)).json()["accounts"]
    assert [a["id"] for a in listed] == [ids[0], ids[2], ids[1]]
    assert client.patch(f"/api/mail/accounts/{ids[0]}", headers=auth(admin),
                        json={"move": "left"}).status_code == 400
    assert client.patch(f"/api/mail/accounts/{ids[0]}", headers=auth(admin),
                        json={}).status_code == 400
    assert client.patch("/api/mail/accounts/999", headers=auth(admin),
                        json={"enabled": False}).status_code == 404


def test_editing_connection_or_password_clears_recorded_error(client, users, tmp_path):
    from server import db
    _, admin = users
    a = add_account(client, admin, **ACCOUNT)

    def set_error():
        with db.get_db(tmp_path / "zatsumu.db") as conn:
            conn.execute("UPDATE mail_accounts SET fetch_error = 'ログインできませんでした',"
                         " fetch_error_at = '2026-10-22T00:00:00+00:00'")

    set_error()
    client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin), json={"label": "インフォ"})
    assert status(client, admin)["last_error"]          # 名前だけなら消さない
    client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin), json={"imap_pass": "new"})
    assert status(client, admin)["last_error"] == ""
    set_error()
    client.patch(f"/api/mail/accounts/{a['id']}", headers=auth(admin),
                 json={"imap_host": "imap9.bics.example"})
    assert status(client, admin)["last_error"] == ""


def test_delete_account_keeps_mails_and_is_audited(client, users, tmp_path):
    _, admin = users
    kurosu = add_account(client, admin, **KUROSU)
    mid = _mail_for(tmp_path, kurosu["id"])
    listed = client.get("/api/mail/accounts", headers=auth(admin)).json()
    assert listed["accounts"][0]["mail_count"] == 1 and listed["deleted_mail_count"] == 0
    r = client.delete(f"/api/mail/accounts/{kurosu['id']}", headers=auth(admin))
    assert r.json() == {"deleted": kurosu["id"], "mails_kept": 1}
    assert accounts_rows(tmp_path) == []
    d = client.get(f"/api/mail/{mid}", headers=auth(admin))
    assert d.status_code == 200 and d.json()["account_id"] is None
    listed = client.get("/api/mail/accounts", headers=auth(admin)).json()
    assert listed == {"accounts": [], "deleted_mail_count": 1, "max_count": 10}
    assert client.delete(f"/api/mail/accounts/{kurosu['id']}",
                         headers=auth(admin)).status_code == 404
    import datetime as _dt
    csv_text = client.get("/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
                          headers=auth(admin)).text
    assert "受信するメールアドレスを追加" in csv_text
    assert "受信するメールアドレスを削除" in csv_text and "mails_kept=1" in csv_text


def test_account_routes_come_before_mail_id(client):
    from server import app as m
    paths = [getattr(r, "path", "") for r in m.app.routes]
    for p in ("/api/mail/accounts", "/api/mail/accounts/{account_id}",
              "/api/mail/accounts/{account_id}/test"):
        assert paths.index(p) < paths.index("/api/mail/{mail_id}"), p


def test_mail_page_has_account_ui(client):
    page = client.get("/mail").text
    for marker in ('id="acct-card"', 'id="acct-list"', 'id="filter-acct"', 'id="m-from"',
                   'id="acct-imap-pass" autocomplete="new-password"',
                   'api(`/api/mail/accounts/${a.id}/test`'):
        assert marker in page, marker
    # 名前・アドレス・失敗の理由は esc() を通して入れる
    assert "宛先: ${esc(m.account_label)}" in page
    assert "<b>${esc(a.label)}</b> <small>${esc(a.address)}</small>" in page
    assert "受信に失敗しました: ${esc(a.last_error)}" in page
    # 古い1つだけの受信サーバの欄は無い (アドレスごとに入れる)
    assert 'id="set-imap-host"' not in page and 'imap_host: $("set-imap-host")' not in page


# ---------- 前の版に戻ったとき用に、旧設定もアドレス1と同じにしておく ----------

def _legacy_state(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key IN"
        " ('mail_state_uidvalidity', 'mail_state_last_uid')")}


def test_fetch_on_first_account_also_moves_the_legacy_position(tmp_path, monkeypatch):
    """アドレス1で受信したら、前の版が読む続きの位置も進める (前の版に戻っても、新しい版が
    飛ばした自分の返信の控えを読み直して取り込まない)."""
    from server import db, mail
    path = tmp_path / "old.db"
    _legacy_db(path, mail_auto_draft=0)
    own_copy = raw_mail(122, frm="info@bics.example", to="kurosu@bics.example")
    box = Box({121: raw_mail(121), 122: own_copy}, uidvalidity=b"777")
    use_boxes(monkeypatch, {"imap.bics.example": box})
    conn = db.connect(path)
    added = mail.fetch_mail_locked(conn, db.get_settings(conn))
    assert [a["message_id"] for a in added] == ["<u121@example.com>"]   # 控えは飛ばす
    assert conn.execute("SELECT state_last_uid FROM mail_accounts").fetchone()[0] == 122
    assert _legacy_state(conn) == {"mail_state_uidvalidity": "777", "mail_state_last_uid": "122"}
    # UIDVALIDITY が変わったときは、それも書き写す
    box.uidvalidity = b"888"
    box.mails = {5: raw_mail(5)}
    mail.fetch_mail_locked(conn, db.get_settings(conn))
    assert _legacy_state(conn) == {"mail_state_uidvalidity": "888", "mail_state_last_uid": "5"}
    # 旧設定が別の受信先を指しているとき (前の版の画面で変えたなど) は、進めない
    conn.execute("UPDATE settings SET value = 'other@bics.example' WHERE key = 'imap_user'")
    conn.commit()
    box.mails = {5: raw_mail(5), 6: raw_mail(6)}
    mail.fetch_mail_locked(conn, db.get_settings(conn))
    assert conn.execute("SELECT state_last_uid FROM mail_accounts").fetchone()[0] == 6
    assert _legacy_state(conn)["mail_state_last_uid"] == "5"
    conn.close()


def test_fetch_on_other_accounts_does_not_touch_the_legacy_position(client, users, tmp_path,
                                                                    monkeypatch):
    from server import db, mail
    _, admin = users
    other = add_account(client, admin, **KUROSU)
    box = Box({50: raw_mail(50, to="kurosu@bics.example")})
    use_boxes(monkeypatch, {"imap2.bics.example": box})
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES"
                     " ('mail_state_last_uid', '7')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     (db.MAIL_LEGACY_ACCOUNT_KEY, str(other["id"] + 100)))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.fetch_mail_locked(conn, {**db.get_settings(conn), "mail_auto_draft": 0})
        assert _legacy_state(conn)["mail_state_last_uid"] == "7"


def test_editing_first_account_updates_the_legacy_settings(client, users, tmp_path):
    """アドレス1 (旧設定から作ったもの) の受信の設定を変えたら、旧設定にも書き写す
    (前の版に戻っても、新しいパスワード・受信先で受信する)。ほかのアドレスでは変えない."""
    from server import db
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_host": "imap.bics.example", "imap_user": "info@bics.example",
                       "imap_pass": "old-pw", "mail_from": "info@bics.example"})
    _add_mail(tmp_path, n=1)                   # この接続で旧設定から1つ目のアドレスを作る
    legacy = accounts_rows(tmp_path)[0]["id"]
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES"
                     " ('mail_state_uidvalidity', '777'), ('mail_state_last_uid', '120')")

    def legacy_settings():
        with db.get_db(tmp_path / "zatsumu.db") as conn:
            s = db.get_settings(conn)
            return s, _legacy_state(conn)

    # パスワードだけ: 旧設定のパスワードを変え、続きの位置はそのまま
    r = client.patch(f"/api/mail/accounts/{legacy}", headers=auth(admin),
                     json={"imap_pass": "new-pw", "label": "インフォ",
                           "from_addr": "ビックス <info@bics.example>"})
    assert r.status_code == 200, r.text
    s, state = legacy_settings()
    assert s["imap_pass"] == "new-pw"
    assert s["mail_from"] == "info@bics.example"      # 共通の差出人は変えない
    assert state == {"mail_state_uidvalidity": "777", "mail_state_last_uid": "120"}
    # 受信先を変えたら、旧設定も変えて、続きの位置も消す
    r = client.patch(f"/api/mail/accounts/{legacy}", headers=auth(admin),
                     json={"imap_host": "imap9.bics.example", "imap_folder": "INBOX.cases",
                           "imap_ssl": False, "imap_port": "143", "save_sent": False,
                           "imap_sent_folder": "INBOX.Sent"})
    assert r.status_code == 200, r.text
    s, state = legacy_settings()
    assert (s["imap_host"], s["imap_folder"], s["imap_use_ssl"], s["imap_port"],
            s["mail_save_sent"], s["imap_sent_folder"]) == (
        "imap9.bics.example", "INBOX.cases", 0, "143", 0, "INBOX.Sent")
    assert state == {"mail_state_uidvalidity": "", "mail_state_last_uid": "0"}
    # ほかのアドレスを変えても、旧設定は変えない
    other = add_account(client, admin, **KUROSU)
    client.patch(f"/api/mail/accounts/{other['id']}", headers=auth(admin),
                 json={"imap_pass": "kurosu-new", "imap_host": "imap3.bics.example"})
    s, _ = legacy_settings()
    assert (s["imap_pass"], s["imap_host"]) == ("new-pw", "imap9.bics.example")
