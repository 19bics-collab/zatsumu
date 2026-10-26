"""問い合わせの種類で自動仕分け・担当のテスト.

受信時に種類 (清掃のご依頼など) を決め、種類ごとの担当 (設定) を入れる。
AI を使うときは優先度と同じ1回の依頼で種類も選ばせ、一覧に無い種類は「その他」にする。
AI を使わないときは設定のキーワードで決める。ネットワークには出ない
(AI・IMAP は偽物に差し替える)。
"""
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth
from tests.test_mail_ai import KEY, FakeAnthropic, _resp, _status_error

DEFAULT_CATEGORIES = ["清掃のご依頼", "予約・宿泊のお問い合わせ", "請求・お支払い",
                      "運営代行のご相談", "営業・宣伝", "その他"]


@pytest.fixture(autouse=True)
def reset_tz():
    from server import tz
    tz.set_tz("Asia/Tokyo")
    yield
    tz.set_tz("Asia/Tokyo")


@pytest.fixture(autouse=True)
def reset_ai_errors():
    from server import mail
    mail.clear_ai_errors()
    yield
    mail.clear_ai_errors()


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


@pytest.fixture()
def fake_ai(monkeypatch):
    from server import mail
    FakeAnthropic.instances = []
    FakeAnthropic.calls = []
    FakeAnthropic.reply = _resp("{}")
    monkeypatch.setattr(mail.anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


def _settings(**kw):
    from server import db
    return {**db.INT_SETTINGS, **db.STR_SETTINGS, "anthropic_api_key": "", **kw}


def _add_user(tmp_path, name, active=True):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        u = db.create_user(conn, name)
        if not active:
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (u["id"],))
    return u


def _set_mail(tmp_path, mid, **cols):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        for k, v in cols.items():
            conn.execute(f"UPDATE mails SET {k} = ? WHERE id = ?", (v, mid))


def _mail_row(tmp_path, mid):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return dict(conn.execute("SELECT * FROM mails WHERE id = ?", (mid,)).fetchone())


MAIL = {"from_addr": "guest@example.com", "from_name": "山田",
        "subject": "お問い合わせ", "body": "よろしくお願いします"}


# ---------- 設定 (種類の一覧・言葉・担当) ----------

def test_default_settings_are_valid_and_stable():
    """初期値の言葉は初期値の種類だけを使い、保存の形に整えても変わらない."""
    from server import db, mail
    s = _settings()
    assert mail.category_list(s) == DEFAULT_CATEGORIES
    cleaned = mail.clean_category_settings(
        s["mail_categories"], s["mail_category_keywords"], s["mail_category_assignees"])
    assert cleaned == (db.STR_SETTINGS["mail_categories"],
                       db.STR_SETTINGS["mail_category_keywords"], "")
    assert [c for c, _w in mail.category_keywords(s)] == DEFAULT_CATEGORIES[:5]


def test_category_list_parsing():
    from server import mail
    # 改行・カンマ・読点のどれで区切ってもよい。空の行・同じ名前 (全角/半角の違いも) はまとめる
    s = _settings(mail_categories="清掃, 予約\n\n請求、ｸﾘｰﾆﾝｸﾞ\nクリーニング\n 清掃 ")
    assert mail.category_list(s) == ["清掃", "予約", "請求", "ｸﾘｰﾆﾝｸﾞ", "その他"]
    # 「その他」は消しても入る (書いた場所はそのまま)
    assert mail.category_list(_settings(mail_categories="その他\n清掃")) == ["その他", "清掃"]
    assert mail.category_list(_settings(mail_categories="")) == ["その他"]
    # 保存のときに断る名前が DB に入っていても、画面を壊さず飛ばす
    assert mail.category_list(_settings(mail_categories="未分類\nNone\na:b\n清掃")) == [
        "清掃", "その他"]
    # 設定が無い (古い呼び出し) ときは初期値
    assert mail.category_list({}) == DEFAULT_CATEGORIES


def test_clean_category_settings_normalizes():
    from server import mail
    users = [{"id": 1, "name": "黒須 太郎"}, {"id": 2, "name": "田中"}]
    cats, kws, asg = mail.clean_category_settings(
        "清掃のご依頼\r\n運営代行のご相談",
        # 日本語入力のままの全角のコロン・読点、前後の空白、空の行
        "\n 清掃のご依頼：清掃、 リネン ,タオル\n\n運営代行のご相談: 代行\n",
        "運営代行のご相談：黒須太郎\n清掃のご依頼: 田中",
        users=users)
    assert cats == "清掃のご依頼\n運営代行のご相談\nその他"
    assert kws == "清掃のご依頼: 清掃, リネン, タオル\n運営代行のご相談: 代行"
    # 担当の名前は、登録されている名前の表記にそろえる (空白の違いは同じ人)
    assert asg == "運営代行のご相談: 黒須 太郎\n清掃のご依頼: 田中"
    # 種類の表記の違い (空白・全角/半角) は一覧の表記にそろえる
    _c, kws, _a = mail.clean_category_settings("請求・お支払い", "請求・お支払い : 請求", "")
    assert kws == "請求・お支払い: 請求"


@pytest.mark.parametrize("cats,kws,asg,expect", [
    ("清掃\n未分類", "", "", "使えません"),
    ("清掃\nnone", "", "", "使えません"),
    ("清掃:依頼", "", "", "「:」は使えません"),
    ("あ" * 31, "", "", "長すぎます"),
    ("\n".join(f"種類{i}" for i in range(30)), "", "", "30個まで"),
    ("清掃", "清掃 クリーニング", "", "1行目「清掃 クリーニング」の書き方"),
    ("清掃", "\n予約: 予約", "", "2行目の種類「予約」は、種類の一覧にありません"),
    ("清掃", "清掃:", "", "1行目の「:」の後ろが空です"),
    ("清掃", "清掃: " + "あ" * 5000, "", "長すぎます"),
    ("清掃", "", "清掃: 田中, 鈴木", "1人まで"),
    ("清掃", "", "清掃: 田中\n清掃: 鈴木", "2回"),
    ("清掃", "", "予約: 田中", "種類の一覧にありません"),
])
def test_clean_category_settings_rejects(cats, kws, asg, expect):
    from server import mail
    with pytest.raises(ValueError, match=expect):
        mail.clean_category_settings(cats, kws, asg)


def test_clean_category_settings_checks_names_only_with_users():
    from server import mail
    # 人の一覧を渡したときだけ、名前が登録されているかを確かめる
    assert mail.clean_category_settings("清掃", "", "清掃: 誰か")[2] == "清掃: 誰か"
    with pytest.raises(ValueError, match="「誰か」という名前の人が見つかりません"):
        mail.clean_category_settings("清掃", "", "清掃: 誰か", users=[{"id": 1, "name": "田中"}])


# ---------- キーワードで決める (AI を使わないとき) ----------

def test_rule_category_first_matching_line_wins():
    from server import mail
    s = _settings()
    cases = [
        ("清掃の予約をお願いします", "清掃のご依頼"),          # 上の行 (清掃) が先
        ("来週の予約について", "予約・宿泊のお問い合わせ"),
        ("今月のご請求書", "請求・お支払い"),
        ("民泊運営の相談", "運営代行のご相談"),
        ("ＵＮＳＵＢＳＣＲＩＢＥ はこちら", "営業・宣伝"),   # 全角・大文字でも当たる
        ("ｸﾘｰﾆﾝｸﾞのお願い", "清掃のご依頼"),               # 半角カナでも当たる
        ("先日のお礼", "その他"),                           # どれにも当たらない
    ]
    for body, expect in cases:
        assert mail.rule_category(s, {**MAIL, "body": body}) == expect, body
    # 件名と差出人も見る
    assert mail.rule_category(s, {**MAIL, "subject": "リネンの件"}) == "清掃のご依頼"
    assert mail.rule_category(
        s, {**MAIL, "from_addr": "automated@airbnb.com"}) == "予約・宿泊のお問い合わせ"
    # 行の順番を入れ替えれば、先に書いた方が勝つ
    s2 = _settings(mail_category_keywords="予約・宿泊のお問い合わせ: 予約\n清掃のご依頼: 清掃")
    assert mail.rule_category(s2, {**MAIL, "body": "清掃の予約"}) == "予約・宿泊のお問い合わせ"
    # 一覧に無い種類の行は使わない (DB を直接書き換えた等)
    s3 = _settings(mail_category_keywords="消した種類: お問い合わせ")
    assert mail.rule_category(s3, MAIL) == "その他"


def test_classify_without_ai_uses_rules(fake_ai):
    from server import mail
    c = mail.classify_mail(_settings(), {**MAIL, "subject": "至急 シーツの交換"})
    assert c == {"priority": 1, "reason": "「至急」を含む", "source": "rule",
                 "category": "清掃のご依頼"}
    assert fake_ai.calls == []


# ---------- AI で決める ----------

def test_ai_returns_category_in_the_same_request(fake_ai):
    """種類は優先度と同じ1回の依頼で決める (依頼を増やさない)."""
    from server import mail
    fake_ai.reply = _resp('{"priority": 2, "reason": "清掃の依頼", "category": "清掃のご依頼"}')
    s = _settings(anthropic_api_key=KEY)
    c = mail.classify_mail(s, MAIL)
    assert c == {"priority": 2, "reason": "清掃の依頼", "source": "ai",
                 "category": "清掃のご依頼"}
    (params,) = fake_ai.calls
    assert params["model"] == "claude-haiku-5-5"
    system = params["system"]
    # 選べる種類 (設定の順) と、決める目安の言葉を伝える
    assert " / ".join(DEFAULT_CATEGORIES) in system
    assert "清掃のご依頼: 清掃, クリーニング" in system
    assert '"category"' in system
    # 優先度だけを聞く呼び出し (前からの形) も、同じ1回の依頼で動く
    fake_ai.calls.clear()
    assert mail.classify_priority(s, MAIL) == (2, "清掃の依頼", "ai")
    assert len(fake_ai.calls) == 1


@pytest.mark.parametrize("answer,expect", [
    ('"運営代行のご相談"', "運営代行のご相談"),
    ('" 運営代行の ご相談 "', "運営代行のご相談"),     # 空白の違いは一覧の表記にそろえる
    ('"お見積もり"', "その他"),                       # 一覧に無い種類
    ('""', "その他"),
    ('null', "その他"),
    ('["清掃のご依頼"]', "その他"),
    ('"未分類"', "その他"),
])
def test_ai_unknown_category_becomes_other(fake_ai, answer, expect):
    from server import mail
    fake_ai.reply = _resp('{"priority": 1, "reason": "相談", "category": ' + answer + "}")
    c = mail.classify_mail(_settings(anthropic_api_key=KEY), MAIL)
    assert (c["source"], c["priority"], c["category"]) == ("ai", 1, expect)


def test_ai_without_category_key_becomes_other(fake_ai):
    from server import mail
    fake_ai.reply = _resp('{"priority": 3, "reason": "お知らせ"}')
    c = mail.classify_mail(_settings(anthropic_api_key=KEY), {**MAIL, "body": "清掃"})
    assert (c["source"], c["category"]) == ("ai", "その他")


def test_ai_failure_falls_back_to_rules_for_category(fake_ai):
    from server import mail

    def no_credit(params):
        raise _status_error("BadRequestError", 400, "Your credit balance is too low")

    fake_ai.reply = no_credit
    c = mail.classify_mail(_settings(anthropic_api_key=KEY), {**MAIL, "body": "リネンの補充"})
    assert (c["source"], c["category"]) == ("rule", "清掃のご依頼")
    assert "残高" in mail.last_ai_error("classify")
    # 読めない答えのときも同じ
    fake_ai.reply = _resp('{"priority": 7, "category": "清掃のご依頼"}')
    c = mail.classify_mail(_settings(anthropic_api_key=KEY), {**MAIL, "body": "振込のご連絡"})
    assert (c["source"], c["category"]) == ("rule", "請求・お支払い")


# ---------- 受信 (IMAP) で種類と担当が入る ----------

def _fake_inbox(monkeypatch, mails):
    """[(件名, 本文), …] の入った受信箱 (IMAP) の偽物を差し込む."""
    from server import mail

    def raw(uid):
        subject, body = mails[uid - 1]
        m = EmailMessage()
        m["From"] = f"guest{uid}@example.com"
        m["Subject"] = subject
        m["Message-ID"] = f"<c{uid}@example.com>"
        m.set_content(body)
        return m.as_bytes()

    class Box:
        def select(self, folder, readonly=False):
            return ("OK", [str(len(mails)).encode()])

        def response(self, key):
            return ("UIDVALIDITY", [b"1"])

        def uid(self, cmd, *args):
            if cmd == "search":
                return ("OK", [b" ".join(str(u).encode() for u in range(1, len(mails) + 1))])
            return ("OK", [(b"header", raw(int(args[0])))])

        def logout(self):
            pass

    monkeypatch.setattr(mail, "_imap_connect", lambda s: Box())


INBOX = [("清掃のお願い", "来週の清掃をお願いします"),
         ("運営代行について", "民泊運営の相談です"),
         ("先日のお礼", "ありがとうございました"),
         ("請求書", "今月の請求書です")]


def test_fetch_stores_category_and_default_assignee(monkeypatch, tmp_path):
    from server import db, mail
    _fake_inbox(monkeypatch, INBOX)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        kurosu = db.create_user(conn, "黒須 太郎")
        gone = db.create_user(conn, "退職者")
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (gone["id"],))
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "mail_auto_draft": 0,
             # 名前の空白の違いは同じ人。無効にした人・いない人は担当にしない
             "mail_category_assignees": "運営代行のご相談: 黒須太郎\n"
                                        "清掃のご依頼: 退職者\n請求・お支払い: 誰か"}
        added = mail.fetch_new_mail(conn, s)
        rows = {r["subject"]: r for r in conn.execute(
            "SELECT subject, category, assignee_user_id FROM mails")}
    assert len(added) == 4
    assert {k: (r["category"], r["assignee_user_id"]) for k, r in rows.items()} == {
        "清掃のお願い": ("清掃のご依頼", None),
        "運営代行について": ("運営代行のご相談", kurosu["id"]),
        "先日のお礼": ("その他", None),
        "請求書": ("請求・お支払い", None),
    }


def test_fetch_with_ai_uses_one_request_per_mail(fake_ai, monkeypatch, tmp_path):
    from server import db, mail
    _fake_inbox(monkeypatch, INBOX[:3])
    answers = iter(["運営代行のご相談", "予約・宿泊のお問い合わせ", "知らない種類"])
    fake_ai.reply = lambda params: _resp(
        '{"priority": 2, "reason": "通常", "category": "%s"}' % next(answers))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        kurosu = db.create_user(conn, "黒須")
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "anthropic_api_key": KEY, "mail_auto_draft": 0,
             "mail_category_assignees": "運営代行のご相談: 黒須"}
        mail.fetch_new_mail(conn, s)
        rows = conn.execute("SELECT category, assignee_user_id, priority_source FROM mails"
                            " ORDER BY imap_uid").fetchall()
    # 判定は1通につき1回だけ (種類のために依頼を増やさない)
    assert len(fake_ai.calls) == 3
    assert [tuple(r) for r in rows] == [
        ("運営代行のご相談", kurosu["id"], "ai"),
        ("予約・宿泊のお問い合わせ", None, "ai"),
        ("その他", None, "ai"),
    ]


# ---------- 一覧と絞り込み ----------

def test_list_shows_category_and_assignee_and_filters(client, users, tmp_path):
    worker, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    m_clean = _add_mail(tmp_path, n=1)
    m_mine = _add_mail(tmp_path, n=2)
    m_old = _add_mail(tmp_path, n=3)          # この機能より前のメール (種類が空)
    m_done = _add_mail(tmp_path, n=4, status="replied")
    _set_mail(tmp_path, m_clean, category="清掃のご依頼", assignee_user_id=kurosu["id"])
    _set_mail(tmp_path, m_mine, category="運営代行のご相談", assignee_user_id=admin["id"])
    _set_mail(tmp_path, m_done, category="清掃のご依頼")

    def ids(query=""):
        r = client.get("/api/mail" + query, headers=auth(admin))
        assert r.status_code == 200, r.text
        return {m["id"] for m in r.json()}

    rows = {m["id"]: m for m in client.get("/api/mail", headers=auth(admin)).json()}
    assert (rows[m_clean]["category"], rows[m_clean]["assignee_user_id"],
            rows[m_clean]["assignee_name"]) == ("清掃のご依頼", kurosu["id"], "黒須")
    assert (rows[m_old]["category"], rows[m_old]["assignee_user_id"],
            rows[m_old]["assignee_name"]) == ("", None, "")
    # 種類で絞り込む (none = 未分類)。日本語の名前は URL に入れて送る
    assert ids("?category=清掃のご依頼") == {m_clean, m_done}
    assert ids("?category=%E6%B8%85%E6%8E%83%E3%81%AE%E3%81%94%E4%BE%9D%E9%A0%BC") == {
        m_clean, m_done}
    assert ids("?category=none") == {m_old}
    assert ids("?category=知らない種類") == set()
    # 担当で絞り込む (番号・none = 担当なし・me = 自分)
    assert ids(f"?assignee={kurosu['id']}") == {m_clean}
    assert ids("?assignee=none") == {m_old, m_done}
    assert ids("?assignee=me") == {m_mine}
    # ほかの絞り込みと一緒に使える
    assert ids("?status=unhandled&category=清掃のご依頼") == {m_clean}
    assert ids("?status=replied&assignee=none") == {m_done}
    for bad in ("abc", "-1", "１", "1.5"):
        r = client.get(f"/api/mail?assignee={bad}", headers=auth(admin))
        assert r.status_code == 400, bad
    assert client.get("/api/mail?category=none", headers=auth(worker)).status_code == 403


def test_detail_has_category_and_assignee(client, users, tmp_path):
    _, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    mid = _add_mail(tmp_path)
    _set_mail(tmp_path, mid, category="営業・宣伝", assignee_user_id=kurosu["id"])
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert (d["category"], d["assignee_user_id"], d["assignee_name"]) == (
        "営業・宣伝", kurosu["id"], "黒須")
    assert d["body"] == "本文"
    assert client.get("/api/mail/9999", headers=auth(admin)).status_code == 404


# ---------- 担当に選べる人 ----------

def test_assignees_endpoint(client, users, tmp_path):
    from server import app as app_module
    worker, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    _add_user(tmp_path, "退職者", active=False)
    r = client.get("/api/mail/assignees", headers=auth(admin))
    assert r.status_code == 200
    # 有効な人の番号と名前だけ (トークンなどは返さない)
    assert r.json() == sorted(
        [{"id": admin["id"], "name": "boss"}, {"id": worker["id"], "name": "tanaka"},
         {"id": kurosu["id"], "name": "黒須"}], key=lambda u: u["name"])
    assert client.get("/api/mail/assignees", headers=auth(worker)).status_code == 403
    assert client.get("/api/mail/assignees").status_code == 401
    # "assignees" が mail_id (整数) として読まれない (固定の道が {mail_id} より先)
    paths = [getattr(rt, "path", "") for rt in app_module.app.routes]
    assert paths.index("/api/mail/assignees") < paths.index("/api/mail/{mail_id}")


# ---------- 返信画面で変える (PATCH) ----------

def _audits(tmp_path):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [r["detail"] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'mail_update' ORDER BY id")]


def test_patch_category_and_assignee(client, users, tmp_path):
    worker, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    gone = _add_user(tmp_path, "退職者", active=False)
    mid = _add_mail(tmp_path)

    def patch(body):
        return client.patch(f"/api/mail/{mid}", headers=auth(admin), json=body)

    r = patch({"category": " 清掃のご依頼 "})
    assert r.status_code == 200
    assert r.json() == {"updated": mid, "category": "清掃のご依頼",
                        "assignee_user_id": None, "assignee_name": ""}
    # 一覧に無い種類は断る (変わらない)
    r = patch({"category": "お見積もり"})
    assert r.status_code == 400 and "種類の一覧にありません" in r.json()["detail"]
    assert _mail_row(tmp_path, mid)["category"] == "清掃のご依頼"
    # 空 = 未分類に戻す
    assert patch({"category": ""}).json()["category"] == ""
    # 担当: 有効な人だけ。いない人・無効にした人は断る
    r = patch({"assignee_user_id": kurosu["id"]})
    assert r.json()["assignee_user_id"] == kurosu["id"] and r.json()["assignee_name"] == "黒須"
    assert patch({"assignee_user_id": 9999}).status_code == 400
    assert patch({"assignee_user_id": gone["id"]}).status_code == 400
    assert _mail_row(tmp_path, mid)["assignee_user_id"] == kurosu["id"]
    # null = 担当なし
    r = patch({"assignee_user_id": None})
    assert r.status_code == 200 and r.json()["assignee_user_id"] is None
    assert _mail_row(tmp_path, mid)["assignee_user_id"] is None
    # 何も送らないのは今までどおり 400。一般ユーザーは使えない
    assert patch({}).status_code == 400
    assert client.patch(f"/api/mail/{mid}", headers=auth(worker),
                        json={"category": "その他"}).status_code == 403
    # 誰がいつ何に変えたかを監査ログに残す (担当は名前と番号)
    assert _audits(tmp_path) == [
        f"mail={mid} category=清掃のご依頼",
        f"mail={mid} category=未分類",
        f"mail={mid} assignee=黒須({kurosu['id']})",
        f"mail={mid} assignee=なし",
    ]


def test_patch_keeps_current_values_even_if_no_longer_valid(client, users, tmp_path):
    """一覧から消した種類・無効にした人のままにしておくのは認める (ほかの項目を変えられるように)."""
    _, admin = users
    gone = _add_user(tmp_path, "退職者")
    mid = _add_mail(tmp_path)
    _set_mail(tmp_path, mid, category="消した種類", assignee_user_id=gone["id"])
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (gone["id"],))
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"category": "消した種類", "assignee_user_id": gone["id"],
                           "priority": 1})
    assert r.status_code == 200
    assert (r.json()["category"], r.json()["assignee_name"]) == ("消した種類", "退職者")


def test_changing_category_fills_default_assignee(client, users, tmp_path):
    """担当がいないメールの種類を変えると、その種類の担当 (設定) が入る."""
    _, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    tanaka_id = users[0]["id"]
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_category_assignees": "運営代行のご相談: 黒須"})
    assert r.status_code == 200, r.text
    m_free = _add_mail(tmp_path, n=1)
    m_taken = _add_mail(tmp_path, n=2)
    m_explicit = _add_mail(tmp_path, n=3)
    _set_mail(tmp_path, m_taken, assignee_user_id=tanaka_id)

    r = client.patch(f"/api/mail/{m_free}", headers=auth(admin),
                     json={"category": "運営代行のご相談"})
    assert (r.json()["assignee_user_id"], r.json()["assignee_name"]) == (kurosu["id"], "黒須")
    # 担当がいるメールは変えない
    r = client.patch(f"/api/mail/{m_taken}", headers=auth(admin),
                     json={"category": "運営代行のご相談"})
    assert r.json()["assignee_user_id"] == tanaka_id
    # 担当を一緒に送ったら、そちらに従う (担当なしのまま)
    r = client.patch(f"/api/mail/{m_explicit}", headers=auth(admin),
                     json={"category": "運営代行のご相談", "assignee_user_id": None})
    assert r.json()["assignee_user_id"] is None
    # 担当の決まっていない種類なら担当なしのまま
    r = client.patch(f"/api/mail/{m_explicit}", headers=auth(admin),
                     json={"category": "清掃のご依頼"})
    assert r.json()["assignee_user_id"] is None
    assert f"mail={m_free} category=運営代行のご相談, assignee=黒須({kurosu['id']})" \
        in _audits(tmp_path)


def test_deleting_a_user_unassigns_their_mails(client, users, tmp_path):
    _, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    mid = _add_mail(tmp_path)
    _set_mail(tmp_path, mid, assignee_user_id=kurosu["id"])
    assert client.delete(f"/api/users/{kurosu['id']}", headers=auth(admin)).status_code == 200
    row = _mail_row(tmp_path, mid)
    assert row["assignee_user_id"] is None   # メールは消さずに担当なしに戻す


# ---------- 設定の保存 ----------

def test_settings_save_validates_and_normalizes(client, users, tmp_path):
    from server import db
    _, admin = users
    _add_user(tmp_path, "黒須 太郎")
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["mail_category_list"] == DEFAULT_CATEGORIES
    assert s["mail_category_assignees"] == ""

    r = client.patch("/api/settings", headers=auth(admin), json={
        "mail_categories": "清掃のご依頼、運営代行のご相談",
        "mail_category_keywords": "清掃のご依頼：清掃、リネン",
        "mail_category_assignees": "運営代行のご相談:黒須太郎",
    })
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["mail_categories"] == "清掃のご依頼\n運営代行のご相談\nその他"
    assert s["mail_category_list"] == ["清掃のご依頼", "運営代行のご相談", "その他"]
    assert s["mail_category_keywords"] == "清掃のご依頼: 清掃, リネン"
    assert s["mail_category_assignees"] == "運営代行のご相談: 黒須 太郎"

    # 種類を消すと、その種類を書いた行が残っていれば保存しない (何行目か分かる)
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_categories": "清掃のご依頼"})
    assert r.status_code == 400
    assert "種類ごとの担当の1行目の種類「運営代行のご相談」" in r.json()["detail"]
    # 登録されていない名前は保存しない
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"mail_category_assignees": "清掃のご依頼: 誰か"})
    assert r.status_code == 400 and "「誰か」という名前の人" in r.json()["detail"]
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        stored = db.get_settings(conn)
    assert stored["mail_categories"] == "清掃のご依頼\n運営代行のご相談\nその他"
    assert stored["mail_category_assignees"] == "運営代行のご相談: 黒須 太郎"


def test_unchanged_assignees_do_not_block_saving(client, users, tmp_path):
    """担当に書いた人を後から無効にしても、担当の欄を変えなければほかの設定は保存できる."""
    from server import db
    _, admin = users
    kurosu = _add_user(tmp_path, "黒須")
    assert client.patch("/api/settings", headers=auth(admin), json={
        "mail_category_assignees": "運営代行のご相談: 黒須"}).status_code == 200
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (kurosu["id"],))
    s = client.get("/api/settings", headers=auth(admin)).json()
    # メール画面は「保存」で3つとも送る (Windows の改行で届くこともある)
    r = client.patch("/api/settings", headers=auth(admin), json={
        "mail_signature": "署名",
        "mail_categories": s["mail_categories"],
        "mail_category_keywords": s["mail_category_keywords"],
        "mail_category_assignees": s["mail_category_assignees"].replace("\n", "\r\n") + "\n",
    })
    assert r.status_code == 200, r.text
    # 無効にした人は、届いたメールの担当には入らない
    from server import mail
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.default_assignees(conn, db.get_settings(conn)) == {}


# ---------- 前からある DB ----------

def test_existing_db_gets_category_columns(tmp_path):
    """種類・担当の列が無かった頃の DB (本番) を開くと列が足され、前のメールは未分類・担当なし."""
    import sqlite3

    from server import db
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE,"
                " is_admin INTEGER NOT NULL DEFAULT 0)")
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
        replied_by TEXT NOT NULL DEFAULT '', references_hdr TEXT NOT NULL DEFAULT '',
        cc_addr TEXT NOT NULL DEFAULT '', reply_cc TEXT NOT NULL DEFAULT '',
        reply_bcc TEXT NOT NULL DEFAULT '')""")
    old.execute("INSERT INTO users (name, token) VALUES ('黒須', 't1')")
    old.execute("INSERT INTO mails (message_id, from_addr, subject, received_at,"
                " fetched_at) VALUES ('<old@e>', 'a@example.com', '前のメール',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    old.commit()
    old.close()

    for _ in range(2):   # 2回開いても壊れない
        conn = db.connect(path)
        row = conn.execute("SELECT * FROM mails").fetchone()
        assert row["subject"] == "前のメール"
        assert (row["category"], row["assignee_user_id"]) == ("", None)
        assert not conn.in_transaction
        conn.close()
    # 足した列で担当を付けられ、その人を消すと担当なしに戻る
    conn = db.connect(path)
    conn.execute("UPDATE mails SET assignee_user_id = 1, category = 'その他'")
    conn.execute("DELETE FROM users WHERE id = 1")
    conn.commit()
    assert conn.execute("SELECT assignee_user_id FROM mails").fetchone()[0] is None
    conn.close()


# ---------- 画面 ----------

def test_mail_page_has_category_ui(client):
    page = client.get("/mail").text
    for marker in ('id="cat-tabs"', 'id="filter-asg"', 'id="m-category"',
                   'id="m-assignee"', 'id="cat-card"', 'id="set-mail-categories"',
                   'id="set-mail-category-keywords"', 'id="set-mail-category-assignees"',
                   "種類と担当", '"/api/mail/assignees"', "<th>担当</th>",
                   'q.set(key, $(id).value)', 'mail_category_assignees: $(',
                   '["me", "自分の担当"]', '[TAB_NONE, "未分類"'):
        assert marker in page, marker
    # 種類・担当の名前 (設定や登録した人の名前) は esc() か textContent で出す
    assert '<span class="cat">${esc(c)}</span>' in page
    assert "${esc(m.assignee_name)" in page
    assert "o.textContent = label" in page
    assert '$("asg-names").textContent' in page
    # 担当なしは null で送る (0 や "" ではなく)
    assert "assignee_user_id: v ? Number(v) : null" in page
