"""Notionに登録 (問い合わせのメールを Notion のデータベースに1行で入れる) のテスト.

ネットワークには出ない。Notion の API は偽物 (FakeNotion) に差し替え、送った中身
(データベースの読み取り・ページの中身・ファイルのアップロード・ブロックの追加) を確かめる。
設定の秘密 (シークレット) の扱い、二重登録の防ぎ方、失敗の知らせ方、AI の答えの確かめ方
(AI は偽物) も確かめる。
"""
import email.message
import io
import json
import sqlite3
import urllib.error
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth
from tests.test_mail_ai import KEY, FakeAnthropic, _resp

TOKEN = "ntn_" + "A1b2C3d4E5" * 5          # 連携のシークレット (偽物)
DB_ID = "083ed0d82b954f63abdf190d628201ca"
DB_DASHED = "083ed0d8-2b95-4f63-abdf-190d628201ca"
PAGE_ID = "3fa2a068-16fa-81d0-bade-dafdd63e9bd3"
API = "https://api.notion.com/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PDF = b"%PDF-1.4\n% fake pdf\n"
# 届いた日: 2026-10-22 (木) 日本時間 0:30 (世界標準時ではまだ前の日)
RECEIVED = "2026-10-21T15:30:00+00:00"


def _schema():
    """運営代行 案件DB と同じ形の列 (テスト用)."""
    def select(*names):
        return {"options": [{"id": f"o{i}", "name": n, "color": "gray"}
                            for i, n in enumerate(names)]}
    props = {
        "案件名": {"type": "title", "title": {}, "description": "例: 〇〇様_茅ヶ崎戸建"},
        "問い合わせ日": {"type": "date", "date": {}},
        "問い合わせ元": {"type": "rich_text", "rich_text": {}},
        "ステータス": {"type": "select", "select": select("問い合わせ受付", "ヒアリング中", "見送り")},
        "ボール": {"type": "select", "select": select("インビックス", "相手先", "双方")},
        "経路": {"type": "select", "select": select("LINE", "メール", "電話", "HP")},
        "物件種別": {"type": "select", "select": select("戸建", "マンション区分", "一棟", "その他")},
        "所在地": {"type": "rich_text", "rich_text": {}},
        "間取り": {"type": "rich_text", "rich_text": {}},
        "定員数": {"type": "number", "number": {"format": "number"}},
        "メールスレッド": {"type": "url", "url": {}},
        "次回打ち合わせ": {"type": "date", "date": {}},
        "最終更新": {"type": "last_edited_time", "last_edited_time": {}},
    }
    for name, p in props.items():
        p.update(id=name, name=name)
    return {"object": "database", "id": DB_DASHED,
            "title": [{"type": "text", "plain_text": "運営代行 案件DB"}], "properties": props}


class FakeResp:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNotion:
    """Notion の API の偽物。呼ばれた中身を calls に残す.

    fail[(method, path の先頭)] に (状態の番号, 返事, ヘッダ) を並べると、その順に失敗を返す。
    """

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.schema = _schema()
        self.pages = 0
        self.uploads = 0

    def _error(self, url, status, payload, headers=None):
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError(url, status, "error", hdrs,
                                      io.BytesIO(json.dumps(payload).encode()))

    def __call__(self, req, timeout=None):
        url = req.full_url
        assert url.startswith(API + "/")
        path = url[len(API):]
        method = req.get_method()
        headers = {k.lower(): v for k, v in req.header_items()}
        data = req.data
        body = json.loads(data) if headers.get("content-type") == "application/json" else data
        self.calls.append(SimpleNamespace(method=method, path=path, headers=headers,
                                          body=body, timeout=timeout))
        for (m, prefix), queue in self.fail.items():
            if m == method and path.startswith(prefix) and queue:
                status, payload, hdrs = queue.pop(0)
                raise self._error(url, status, payload, hdrs)
        if method == "GET" and path.startswith("/databases/"):
            return FakeResp(self.schema)
        if method == "POST" and path == "/pages":
            self.pages += 1
            pid = PAGE_ID[:-2] + f"{self.pages:02d}"
            return FakeResp({"object": "page", "id": pid,
                             "url": f"https://www.notion.so/Case-{pid.replace('-', '')}"})
        if method == "POST" and path == "/file_uploads":
            self.uploads += 1
            return FakeResp({"object": "file_upload", "status": "pending",
                             "id": f"11111111-2222-3333-4444-{self.uploads:012d}"})
        if method == "POST" and path.startswith("/file_uploads/") and path.endswith("/send"):
            return FakeResp({"object": "file_upload", "id": path.split("/")[2],
                             "status": "uploaded"})
        if method == "PATCH" and path.startswith("/blocks/"):
            return FakeResp({"object": "list", "results": []})
        raise AssertionError(f"思っていない呼び出し: {method} {path}")

    def of(self, method, prefix):
        return [c for c in self.calls if c.method == method and c.path.startswith(prefix)]


@pytest.fixture(autouse=True)
def reset_tz():
    from server import tz
    tz.set_tz("Asia/Tokyo")
    yield
    tz.set_tz("Asia/Tokyo")


@pytest.fixture()
def fake(monkeypatch):
    from server import notion
    f = FakeNotion()
    monkeypatch.setattr(notion, "_urlopen", f)
    f.sleeps = []
    monkeypatch.setattr(notion, "_sleep", f.sleeps.append)
    return f


@pytest.fixture()
def client(tmp_path, monkeypatch, fake):
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
    FakeAnthropic.reply = _resp('{"values": {}}')
    monkeypatch.setattr(mail.anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


def _configure(client, admin, **extra):
    r = client.patch("/api/settings", headers=auth(admin), json={
        "notion_token": TOKEN, "notion_database_id": f"https://www.notion.so/ws/{DB_ID}?v=abc",
        **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _mail(tmp_path, **kw):
    kw.setdefault("subject", "Re: 民泊運営代行について")
    kw.setdefault("from_name", "山田 太郎")
    kw.setdefault("from_addr", "yamada@example.com")
    kw.setdefault("received_at", RECEIVED)
    kw.setdefault("body", "鎌倉市の戸建てで民泊を始めたいです。\n電話: 090-1234-5678")
    return _add_mail(tmp_path, **kw)


def _row(tmp_path, mail_id):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return dict(conn.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone())


def _audits(tmp_path, action):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [r["detail"] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def _text(block):
    kind = block["type"]
    return "".join(t["text"]["content"] for t in block[kind]["rich_text"])


# ---------- 設定の読み取り ----------

@pytest.mark.parametrize("value, expected", [
    (f"https://www.notion.so/myteam/{DB_ID}?v=0123456789abcdef0123456789abcdef&pvs=4", DB_ID),
    (f"https://www.notion.so/Case-DB-{DB_ID}", DB_ID),
    (f"https://app.notion.com/p/{DB_ID}?pvs=204", DB_ID),
    (f"notion.so/{DB_DASHED}", DB_ID),
    (DB_ID.upper(), DB_ID),
    (f"  {DB_DASHED}  ", DB_ID),
    ("", ""),
])
def test_clean_database_id_accepts_urls_and_ids(value, expected):
    from server import notion
    assert notion.clean_database_id(value) == expected


@pytest.mark.parametrize("value", [
    "運営代行 案件DB", "https://www.notion.so/myteam", DB_ID[:31],
    # ?v= の後ろはビューの番号なので、データベースの番号には使わない
    f"https://www.notion.so/myteam?v={DB_ID}",
])
def test_clean_database_id_rejects_other_text(value):
    from server import notion
    with pytest.raises(ValueError, match="URL"):
        notion.clean_database_id(value)


def test_clean_token():
    from server import notion
    assert notion.clean_token(f"  {TOKEN}\n") == TOKEN
    assert notion.clean_token("") == ""
    for bad in ("ntn_short", "ntn_" + "a" * 30 + " b", "ntn_" + "あ" * 30, "x" * 301):
        with pytest.raises(ValueError) as e:
            notion.clean_token(bad)
        assert bad.strip() not in str(e.value)   # 入れた値は文に出さない


def test_parse_mapping():
    from server import notion
    got = notion.parse_mapping(
        "# コメント\n案件名 = 件名\r\n\n問い合わせ日＝受信日\n延床面積(㎡) = 「120.5」\n"
        "ステータス = 「問い合わせ受付」\n提携先 = 「」\nメール列 = メールアドレス\n")
    assert got == [
        {"column": "案件名", "source": "subject", "literal": ""},
        {"column": "問い合わせ日", "source": "received_date", "literal": ""},
        # 列の名前の全角・記号はそのまま (Notion の列の名前と同じでないと入らない)
        {"column": "延床面積(㎡)", "source": "", "literal": "120.5"},
        {"column": "ステータス", "source": "", "literal": "問い合わせ受付"},
        {"column": "提携先", "source": "", "literal": ""},
        {"column": "メール列", "source": "sender_email", "literal": ""},
    ]


@pytest.mark.parametrize("text, message", [
    ("案件名 件名", "1行目"),
    ("= 件名", "1行目"),
    ("案件名 = 件名\n所在地 = 住所", "2行目"),
    ("案件名 = 件名\n案件名 = 差出人", "2回"),
])
def test_parse_mapping_errors(text, message):
    from server import notion
    with pytest.raises(ValueError, match=message):
        notion.parse_mapping(text)


def test_default_mapping_is_valid_and_matches_case_db():
    from server import db, notion
    mapping = notion.parse_mapping(db.STR_SETTINGS["notion_property_map"])
    props = _parsed_props()
    assert [notion._mapping_problem(e, props) for e in mapping] == [""] * len(mapping)


def _parsed_props():
    from server import notion
    f = FakeNotion()
    orig = notion._urlopen
    notion._urlopen = f
    try:
        return notion.get_database({"notion_token": TOKEN, "notion_database_id": DB_ID})["props"]
    finally:
        notion._urlopen = orig


def test_split_text_counts_like_notion():
    from server import notion
    # 絵文字は Notion では2文字分。改行のところで分け、2000 を超えない
    text = ("あ" * 1500 + "\n" + "😀" * 800 + "\n" + "い" * 10)
    chunks = notion.split_text(text)
    # 改行のところで分け、段落の中の改行はそのまま
    assert chunks == ["あ" * 1500, "😀" * 800 + "\n" + "い" * 10]
    chunks = notion.split_text("😀" * 1200)
    assert chunks == ["😀" * 1000, "😀" * 200]
    assert all(sum(notion._u16(c) for c in ch) <= notion.TEXT_MAX for ch in chunks)
    # 改行の無い長い文は、ちょうどの長さで切る
    assert [len(c) for c in notion.split_text("x" * 4500)] == [2000, 2000, 500]
    assert notion.split_text("") == []


def test_find_phone():
    from server import notion
    assert notion.find_phone("お電話: ０９０−１２３４−５６７８ まで") == "090-1234-5678"
    assert notion.find_phone("TEL 045(123)4567") == "045(123)4567"
    assert notion.find_phone("+81-90-1234-5678") == "+81-90-1234-5678"
    # 日付・郵便番号・FAX だけの行・引用の行 (こちらの署名) は電話番号にしない
    assert notion.find_phone("2026-10-22 〒060-0001\nFAX 03-1111-2222\n> TEL 03-9999-8888") == ""


def test_convert_value_types():
    from server import notion
    props = _parsed_props()
    conv = notion._convert
    assert conv("定員数", props["定員数"], "１，２００")[0] == {"number": 1200}
    assert conv("定員数", props["定員数"], "6人")[0] is None
    # 選択肢は全角・半角・大文字小文字の違いだけなら、Notion の表記にそろえる
    assert conv("経路", props["経路"], "ｈｐ")[0] == {"select": {"name": "HP"}}
    value, problem = conv("経路", props["経路"], "FAX")
    assert value is None and "選択肢" in problem and "HP" in problem
    assert conv("問い合わせ日", props["問い合わせ日"], "2026/10/5")[0] == \
        {"date": {"start": "2026-10-05"}}
    assert conv("問い合わせ日", props["問い合わせ日"], "2026-02-30")[0] is None
    assert conv("メールスレッド", props["メールスレッド"], "javascript:alert(1)")[0] is None
    assert conv("最終更新", props["最終更新"], "x")[1].endswith("この画面からは入れられません")
    assert conv("所在地", props["所在地"], "")[0] is None


# ---------- 設定と秘密 ----------

def test_settings_keep_token_secret(client, users, tmp_path):
    _, admin = users
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["notion_token"] == "" and s["notion_token_set"] is False
    assert s["notion_ai_fill"] == 1 and "案件名 = 件名" in s["notion_property_map"]
    s = _configure(client, admin, notion_property_map="  案件名 = 件名  \n\n\nボール = 「相手先」")
    assert s["notion_token"] == "" and s["notion_token_set"] is True
    assert s["notion_database_id"] == DB_ID                  # URL から番号だけ
    assert s["notion_property_map"] == "案件名 = 件名\nボール = 「相手先」"
    # 操作の記録にはシークレットを残さない
    detail = _audits(tmp_path, "settings_update")[-1]
    assert TOKEN not in detail and "notion_token=***" in detail
    # おかしな値は保存しない (シークレットの値は文に出さない)
    r = client.patch("/api/settings", headers=auth(admin), json={"notion_token": "ntn_x y"})
    assert r.status_code == 400 and "ntn_x y" not in r.text
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"notion_database_id": "案件DB"})
    assert r.status_code == 400
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"notion_property_map": "案件名 = 件名\n所在地 = 住所"})
    assert r.status_code == 400 and "2行目" in r.json()["detail"]
    # 消せる (空にすると「Notionに登録」は使えなくなる)
    s = client.patch("/api/settings", headers=auth(admin), json={"notion_token": ""}).json()
    assert s["notion_token_set"] is False
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert db.get_settings(conn)["notion_token"] == ""


def test_safe_error_hides_notion_token():
    from server import notify
    s = {"notion_token": TOKEN}
    assert TOKEN not in notify.safe_error(RuntimeError(f"bad {TOKEN}"), s)


# ---------- 接続テスト ----------

def test_test_notion_requires_admin_and_settings(client, users):
    worker, admin = users
    assert client.post("/api/settings/test-notion", headers=auth(worker)).status_code == 403
    r = client.post("/api/settings/test-notion", headers=auth(admin))
    assert r.status_code == 400 and "シークレット" in r.json()["detail"]
    client.patch("/api/settings", headers=auth(admin), json={"notion_token": TOKEN})
    r = client.post("/api/settings/test-notion", headers=auth(admin))
    assert r.status_code == 400 and "URL" in r.json()["detail"]


def test_test_notion_lists_columns_and_mapping_problems(client, users, fake):
    _, admin = users
    _configure(client, admin, notion_property_map="\n".join([
        "案件名 = 件名", "問い合わせ日 = 受信日", "ステータス = 「受付」", "最終更新 = 受信日時",
        "定員数 = 差出人", "無い列 = 件名", "ボール = 「インビックス」"]))
    r = client.post("/api/settings/test-notion", headers=auth(admin))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "運営代行 案件DB" and d["ai"] is False
    get = fake.of("GET", "/databases/")[0]
    assert get.path == f"/databases/{DB_DASHED}"
    assert get.headers["authorization"] == f"Bearer {TOKEN}"
    assert get.headers["notion-version"] == "2022-06-28"
    cols = {p["name"]: p for p in d["properties"]}
    assert d["properties"][0]["name"] == "案件名"            # タイトルが先頭
    assert cols["案件名"]["label"] == "タイトル" and cols["案件名"]["fill"] == "件名"
    assert cols["ボール"]["fill"] == "「インビックス」"
    assert cols["所在地"]["fill"] == "入れない"               # AI を使わないとき
    assert cols["最終更新"]["fill"] == "入れられない列です"
    probs = "\n".join(d["problems"])
    assert "「ステータス」の選択肢に「受付」が無い" in probs
    assert "「最終更新」は最終更新日時の列" in probs
    assert "「定員数」は数の列なので、差出人は入れられません" in probs
    assert "「無い列」という列がありません" in probs
    # Notion には何も書き込まない
    assert [c.method for c in fake.calls] == ["GET"]


@pytest.mark.parametrize("status, code, expected", [
    (401, "unauthorized", "シークレット"),
    (403, "restricted_resource", "権限"),
    (404, "object_not_found", "連携(インテグレーション)が招待されていません"),
    (400, "validation_error", "データベースそのもの"),
    (503, "service_unavailable", "一時的な問題"),
])
def test_test_notion_errors_are_plain(client, users, fake, status, code, expected):
    _, admin = users
    _configure(client, admin)
    fake.fail[("GET", "/databases/")] = [
        (status, {"object": "error", "status": status, "code": code,
                  "message": f"Something about {TOKEN}"}, None)]
    r = client.post("/api/settings/test-notion", headers=auth(admin))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert expected in detail
    assert TOKEN not in detail                              # 秘密は伏せる


def test_rate_limit_waits_and_retries(client, users, fake):
    _, admin = users
    _configure(client, admin)
    limited = (429, {"object": "error", "code": "rate_limited", "message": "slow down"},
               {"Retry-After": "3"})
    fake.fail[("GET", "/databases/")] = [limited]
    assert client.post("/api/settings/test-notion", headers=auth(admin)).status_code == 200
    assert fake.sleeps == [3.0]
    # 待つ秒数には上限がある。やり直しても混んでいたら、少し待ってから押してもらう
    fake.sleeps.clear()
    fake.fail[("GET", "/databases/")] = [
        (429, {"code": "rate_limited"}, {"Retry-After": "120"})] * 3
    r = client.post("/api/settings/test-notion", headers=auth(admin))
    assert r.status_code == 502 and "混み合っています" in r.json()["detail"]
    from server import notion
    assert fake.sleeps == [notion.RETRY_AFTER_MAX] * notion.RATE_LIMIT_RETRIES


def test_network_error_is_plain(client, users, monkeypatch):
    from server import notion
    _, admin = users
    _configure(client, admin)

    def down(req, timeout=None):
        raise urllib.error.URLError(OSError("Network is unreachable"))
    monkeypatch.setattr(notion, "_urlopen", down)
    r = client.post("/api/settings/test-notion", headers=auth(admin))
    assert r.status_code == 502 and "つながりませんでした" in r.json()["detail"]

    def slow(req, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(notion, "_urlopen", slow)
    with pytest.raises(notion.NotionError, match="登録できている場合がある"):
        notion._request({"notion_token": TOKEN}, "POST", "/pages", body={})


# ---------- 登録 ----------

def test_register_requires_settings_and_admin(client, users, tmp_path, fake):
    worker, admin = users
    mid = _mail(tmp_path)
    assert client.post(f"/api/mail/{mid}/notion", headers=auth(worker)).status_code == 403
    r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin))
    assert r.status_code == 400 and "Notionの設定がまだです" in r.json()["detail"]
    _configure(client, admin)
    assert client.post("/api/mail/9999/notion", headers=auth(admin)).status_code == 404
    assert fake.calls == []


def test_register_creates_page_from_mapping(client, users, tmp_path, fake):
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin))   # 中身なしでも登録できる
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["notion_page_url"].startswith("https://www.notion.so/Case-")
    assert d["notion_registered_by"] == "boss" and d["database_title"] == "運営代行 案件DB"
    assert d["warnings"] == [] and d["ai_used"] is False and d["attachments_uploaded"] == 0

    page = fake.of("POST", "/pages")[0]
    assert page.headers["notion-version"] == "2022-06-28"
    assert page.body["parent"] == {"database_id": DB_DASHED}
    props = page.body["properties"]
    assert props["案件名"] == {"title": [{"type": "text", "text": {"content": "民泊運営代行について"}}]}
    # 受信日は日本時間の日付 (世界標準時ではまだ前の日)
    assert props["問い合わせ日"] == {"date": {"start": "2026-10-22"}}
    assert props["問い合わせ元"]["rich_text"][0]["text"]["content"] == "山田 太郎 <yamada@example.com>"
    assert props["ステータス"] == {"select": {"name": "問い合わせ受付"}}
    assert props["ボール"] == {"select": {"name": "インビックス"}}
    assert set(props) == {"案件名", "問い合わせ日", "問い合わせ元", "ステータス", "ボール"}
    texts = [(b["type"], _text(b)) for b in page.body["children"]]
    assert texts[:5] == [
        ("heading_2", "問い合わせメール"),
        ("bulleted_list_item", "差出人: 山田 太郎 <yamada@example.com>"),
        ("bulleted_list_item", "受信日時: 2026-10-22 00:30"),
        ("bulleted_list_item", "件名: Re: 民泊運営代行について"),
        ("heading_3", "本文"),
    ]
    assert texts[5] == ("paragraph", "鎌倉市の戸建てで民泊を始めたいです。\n電話: 090-1234-5678")
    assert len(texts) == 6                                    # 添付が無ければ添付の見出しも無い

    row = _row(tmp_path, mid)
    assert row["notion_page_url"] == d["notion_page_url"]
    assert row["notion_page_id"].startswith(PAGE_ID[:-2]) and row["notion_registered_by"] == "boss"
    assert row["notion_registered_at"]
    audit = _audits(tmp_path, "mail_notion")
    assert len(audit) == 1 and audit[0].startswith(f"mail={mid} page=") and "again" not in audit[0]
    # 返信画面でも登録したページが分かる
    m = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert m["notion_page_url"] == d["notion_page_url"] and m["notion_registered_by"] == "boss"


def test_register_body_is_split_and_capped(client, users, tmp_path, fake):
    from server import notion
    _, admin = users
    _configure(client, admin, notion_property_map="案件名 = 件名\n問い合わせ元 = 本文")
    body = ("😀" * 700 + "\n") * 40 + "\x00おわり"   # 長い・絵文字・制御文字
    mid = _mail(tmp_path, body=body, subject="")
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    page = fake.of("POST", "/pages")[0].body
    # 件名が無ければ「（件名なし）」。本文を文字の列に入れるときは 2000 文字まで
    assert _text({"type": "x", "x": {"rich_text": page["properties"]["案件名"]["title"]}}) == "（件名なし）"
    cell = page["properties"]["問い合わせ元"]["rich_text"][0]["text"]["content"]
    assert sum(notion._u16(c) for c in cell) <= notion.TEXT_MAX and cell.endswith("…")
    paragraphs = [b for b in page["children"] if b["type"] == "paragraph"]
    for b in paragraphs:
        assert all(sum(notion._u16(c) for c in t["text"]["content"]) <= notion.TEXT_MAX
                   for t in b["paragraph"]["rich_text"])
    joined = "".join(_text(b) for b in paragraphs[:-1])
    assert len(joined) <= notion.BODY_MAX_CHARS and "\x00" not in joined
    assert "ここまでにしています" in _text(paragraphs[-1])
    assert len(page["children"]) <= notion.CHILDREN_MAX
    assert d["warnings"] == []


def test_register_with_attachments_uses_file_upload(client, users, tmp_path, fake):
    from server import db, mail
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_attachments(conn, tmp_path / "mail_attachments", mid, [
            {"filename": "間取り図.png", "content_type": "image/png", "size": len(PNG),
             "data": PNG, "skip": ""},
            {"filename": "見積書.pdf", "content_type": "application/octet-stream",
             "size": len(PDF), "data": PDF, "skip": ""},
            {"filename": "動画.mov", "content_type": "video/quicktime", "size": 52 * 2 ** 20,
             "data": None, "skip": "too_large"},
        ], "2026-10-22T00:00:00+00:00")
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert d["attachments_uploaded"] == 2
    assert len(d["attachment_notes"]) == 1 and d["attachment_notes"][0].startswith("動画.mov: 載せていません。大きすぎる")
    # 順番: データベースを読む → ページを作る → ファイルごとに受付・送る → ブロックを足す
    order = [(c.method, c.path.split("/")[1]) for c in fake.calls]
    assert order == [("GET", "databases"), ("POST", "pages"),
                     ("POST", "file_uploads"), ("POST", "file_uploads"),
                     ("POST", "file_uploads"), ("POST", "file_uploads"),
                     ("PATCH", "blocks"), ("PATCH", "blocks")]
    page = fake.of("POST", "/pages")[0].body
    assert _text(page["children"][-1]) == "添付ファイル"
    created = [c.body for c in fake.calls if c.path == "/file_uploads"]
    assert created == [
        {"mode": "single_part", "filename": "間取り図.png", "content_type": "image/png"},
        # 中身の種類が分からなかったものは、名前の拡張子から決める
        {"mode": "single_part", "filename": "見積書.pdf", "content_type": "application/pdf"},
    ]
    sends = [c for c in fake.calls if c.path.endswith("/send")]
    assert sends[0].path == "/file_uploads/11111111-2222-3333-4444-000000000001/send"
    assert sends[0].headers["content-type"].startswith("multipart/form-data; boundary=")
    assert "application/json" not in sends[0].headers["content-type"]
    assert sends[0].headers["notion-version"] == "2022-06-28"
    assert 'name="file"; filename="間取り図.png"'.encode() in sends[0].body
    assert PNG in sends[0].body and PDF in sends[1].body
    blocks = fake.of("PATCH", "/blocks/")
    page_id = _row(tmp_path, mid)["notion_page_id"]
    assert blocks[0].path == f"/blocks/{page_id}/children"
    files = blocks[0].body["children"]
    assert [b["type"] for b in files] == ["image", "pdf"]
    assert files[0]["image"]["file_upload"] == {"id": "11111111-2222-3333-4444-000000000001"}
    assert files[0]["image"]["type"] == "file_upload"
    assert files[1]["pdf"]["caption"][0]["text"]["content"] == "見積書.pdf"
    notes = blocks[1].body["children"]
    assert [b["type"] for b in notes] == ["bulleted_list_item"]
    assert _text(notes[0]).startswith("📎 動画.mov")
    assert "attachments=3" in _audits(tmp_path, "mail_notion")[0]


def test_attachment_problems_do_not_stop_the_rest(client, users, tmp_path, fake, monkeypatch):
    from server import db, mail, notion
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_attachments(conn, tmp_path / "mail_attachments", mid, [
            {"filename": n, "content_type": "image/png", "size": len(data), "data": data,
             "skip": ""} for n, data in (("大きい.png", PNG * 4), ("断られる.png", PNG),
                                         ("届く.png", PNG), ("消えた.png", PNG))
        ], "2026-10-22T00:00:00+00:00")
    (tmp_path / "mail_attachments" / str(mid) / "4").unlink()
    monkeypatch.setattr(notion, "UPLOAD_MAX_BYTES", len(PNG) * 2)   # 20MB の代わり
    fake.fail[("POST", "/file_uploads")] = [
        (400, {"code": "validation_error", "message": "The file is too large for this workspace"},
         None)]
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert d["attachments_uploaded"] == 1
    notes = d["attachment_notes"]
    assert notes[0].startswith("大きい.png: 20MBより大きい")
    assert notes[1].startswith("断られる.png: 載せられませんでした") and "5MB" in notes[1]
    assert notes[2].startswith("消えた.png: ファイルが見つからない")
    assert _row(tmp_path, mid)["notion_page_url"]          # ページの登録はそのまま


def test_append_falls_back_to_one_by_one(client, users, tmp_path, fake):
    from server import db, mail
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_attachments(conn, tmp_path / "mail_attachments", mid, [
            {"filename": f"{i}.png", "content_type": "image/png", "size": len(PNG),
             "data": PNG, "skip": ""} for i in range(2)], "2026-10-22T00:00:00+00:00")
    # まとめて足すのと、1つ目を1つずつ足すのを断る
    bad = (400, {"code": "validation_error", "message": "expired"}, None)
    fake.fail[("PATCH", "/blocks/")] = [bad, bad]
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert d["attachments_uploaded"] == 1
    assert d["attachment_notes"] == [d["attachment_notes"][0]]
    assert d["attachment_notes"][0].startswith("0.png: 載せられませんでした")


def test_second_press_needs_again(client, users, tmp_path, fake):
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    first = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin), json={"again": False})
    assert r.status_code == 409
    assert "もうNotionに登録しています" in r.json()["detail"] and "boss" in r.json()["detail"]
    assert fake.pages == 1                                    # 2行目は作らない
    second = client.post(f"/api/mail/{mid}/notion", headers=auth(admin), json={"again": True})
    assert second.status_code == 200 and fake.pages == 2
    assert second.json()["notion_page_url"] != first["notion_page_url"]
    assert _row(tmp_path, mid)["notion_page_url"] == second.json()["notion_page_url"]
    audit = _audits(tmp_path, "mail_notion")
    assert len(audit) == 2 and "again=1" in audit[1]


def test_same_mail_is_not_registered_twice_at_once(client, users, tmp_path, fake):
    from server import notion
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    with notion.registering(mid):   # ほかの画面が登録しているところ
        r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin))
        assert r.status_code == 409 and "登録しているところ" in r.json()["detail"]
        # ほかのメールは登録できる
        other = _mail(tmp_path, message_id="<other@example.com>")
        assert client.post(f"/api/mail/{other}/notion", headers=auth(admin)).status_code == 200
    assert fake.pages == 1
    assert client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).status_code == 200


def test_failed_page_is_not_recorded(client, users, tmp_path, fake):
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    fake.fail[("POST", "/pages")] = [
        (400, {"code": "validation_error", "message": f"body failed validation {TOKEN}"}, None)]
    r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin))
    assert r.status_code == 502
    assert "Notionに登録できませんでした" in r.json()["detail"] and TOKEN not in r.json()["detail"]
    assert _row(tmp_path, mid)["notion_page_url"] == "" and _audits(tmp_path, "mail_notion") == []
    # 直ったら押し直せる
    assert client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).status_code == 200


def test_register_does_not_hold_db_lock_during_network(client, users, tmp_path, fake):
    """Notion とのやりとりの間、DB の書き込みロックを持たない (ほかの人の操作を止めない)."""
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    seen = []
    orig = fake.__call__

    def check(req, timeout=None):
        other = sqlite3.connect(tmp_path / "zatsumu.db", timeout=0.1)
        try:
            other.execute("INSERT INTO settings (key, value) VALUES ('lock_probe', '1')"
                          " ON CONFLICT(key) DO UPDATE SET value = '1'")
            other.commit()
            seen.append(True)
        finally:
            other.close()
        return orig(req, timeout)
    from server import notion
    notion._urlopen = check
    try:
        assert client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).status_code == 200
    finally:
        notion._urlopen = fake
    assert len(seen) == len(fake.calls) >= 2
    assert _row(tmp_path, mid)["notion_page_url"]


def test_mapping_problems_are_warnings(client, users, tmp_path, fake):
    _, admin = users
    _configure(client, admin, notion_property_map="案件名 = 件名\nステータス = 「受付」\n無い列 = 件名\n"
                                                   "問い合わせ元 = 「」")
    mid = _mail(tmp_path)
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert any("「受付」が無い" in w for w in d["warnings"])
    assert any("「無い列」という列がありません" in w for w in d["warnings"])
    props = fake.of("POST", "/pages")[0].body["properties"]
    assert set(props) == {"案件名"}                            # 「」は空のまま


# ---------- AI ----------

def test_ai_fills_columns_with_checked_values(client, users, tmp_path, fake, fake_ai):
    _, admin = users
    _configure(client, admin, anthropic_api_key=KEY)
    fake_ai.reply = _resp(json.dumps({"values": {
        "案件名": "山田様_鎌倉市戸建",
        "問い合わせ元": "山田 太郎 様 / TEL 090-1234-5678",
        "所在地": "神奈川県鎌倉市",
        "定員数": "6",
        "物件種別": "戸建",
        "経路": "FAX",                  # 選択肢に無い → 入れない
        "間取り": 3,                     # 文字の列に数 → 入れない
        "ステータス": "見送り",           # 決まった文字の列 (AI には聞いていない) → 入れない
        "問い合わせ日": "2020-01-01",     # 受信日の列 (聞いていない) → 入れない
        "次回打ち合わせ": "来週",          # 日付でない → 入れない
        "作り話の列": "x",
    }}, ensure_ascii=False))
    mid = _mail(tmp_path)
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert d["ai_used"] is True and d["ai_error"] == "" and d["warnings"] == []
    props = fake.of("POST", "/pages")[0].body["properties"]
    assert props["案件名"]["title"][0]["text"]["content"] == "山田様_鎌倉市戸建"
    assert props["問い合わせ元"]["rich_text"][0]["text"]["content"] == "山田 太郎 様 / TEL 090-1234-5678"
    assert props["所在地"]["rich_text"][0]["text"]["content"] == "神奈川県鎌倉市"
    assert props["定員数"] == {"number": 6}
    assert props["物件種別"] == {"select": {"name": "戸建"}}
    assert props["ステータス"] == {"select": {"name": "問い合わせ受付"}}
    assert props["問い合わせ日"] == {"date": {"start": "2026-10-22"}}
    assert not {"経路", "間取り", "次回打ち合わせ", "作り話の列"} & set(props)
    # 判定用の安いモデルに、列の種類と選択肢を見せて聞く (決まった列は聞かない)
    call = fake_ai.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    prompt = call["messages"][0]["content"]
    assert '"物件種別": 選択（次のどれか1つ）: 戸建 / マンション区分 / 一棟 / その他' in prompt
    assert "例: 〇〇様_茅ヶ崎戸建" in prompt and prompt.index('"案件名"') < prompt.index('"所在地"')
    assert '"ステータス"' not in prompt and '"問い合わせ日"' not in prompt
    assert '"最終更新"' not in prompt
    assert "推測" in call["system"]


def test_ai_failure_still_registers_with_mapping(client, users, tmp_path, fake, fake_ai):
    _, admin = users
    _configure(client, admin, anthropic_api_key=KEY)

    def boom(params):
        raise RuntimeError(f"key {KEY} rejected")
    fake_ai.reply = boom
    mid = _mail(tmp_path)
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert d["ai_used"] is False and d["ai_error"] and KEY not in d["ai_error"]
    props = fake.of("POST", "/pages")[0].body["properties"]
    # AI が使えなければ、決めた値 (件名・差出人) を入れる
    assert props["案件名"]["title"][0]["text"]["content"] == "民泊運営代行について"
    assert props["問い合わせ元"]["rich_text"][0]["text"]["content"] == "山田 太郎 <yamada@example.com>"


def test_ai_can_be_turned_off_and_needs_key(client, users, tmp_path, fake, fake_ai):
    _, admin = users
    _configure(client, admin)   # AI のキーなし
    mid = _mail(tmp_path)
    assert client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).status_code == 200
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY, "notion_ai_fill": False})
    other = _mail(tmp_path, message_id="<n2@example.com>")
    d = client.post(f"/api/mail/{other}/notion", headers=auth(admin)).json()
    assert d["ai_used"] is False and fake_ai.calls == []


def test_clean_ai_values_rejects_bad_shapes():
    from server import mail, notion
    props = _parsed_props()
    with pytest.raises(mail.AIError):
        notion.clean_ai_values("わかりません", props, ["所在地"])
    with pytest.raises(mail.AIError):
        notion.clean_ai_values('{"values": ["所在地"]}', props, ["所在地"])
    got = notion.clean_ai_values(
        '前置き {"values": {"所在地": "  ", "定員数": true, "経路": ["HP"],'
        ' "案件名": "一行目\\n二行目", "問い合わせ日": "2026-10-23"}} 後書き',
        props, ["所在地", "定員数", "経路", "案件名", "問い合わせ日"])
    assert got == {"案件名": {"title": [{"type": "text", "text": {"content": "一行目 二行目"}}]},
                   "問い合わせ日": {"date": {"start": "2026-10-23"}}}


# ---------- DB・画面 ----------

def test_existing_db_gets_notion_columns(tmp_path):
    """前の版の DB (Notion の列が無い) を開くと足され、2回開いても壊れない."""
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
        priority_reason TEXT NOT NULL DEFAULT '', priority_source TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unhandled', draft_reply TEXT NOT NULL DEFAULT '',
        draft_source TEXT NOT NULL DEFAULT '', draft_generated_at TEXT,
        reply_subject TEXT NOT NULL DEFAULT '', reply_body TEXT NOT NULL DEFAULT '',
        replied_at TEXT, replied_by TEXT NOT NULL DEFAULT '',
        references_hdr TEXT NOT NULL DEFAULT '')""")
    old.execute("INSERT INTO mails (message_id, received_at, fetched_at)"
                " VALUES ('<a@e>', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    old.commit()
    old.close()
    for _ in range(2):
        conn = db.connect(path)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(mails)")]
        for col in ("notion_page_id", "notion_page_url", "notion_registered_at",
                    "notion_registered_by"):
            assert cols.count(col) == 1
        row = conn.execute("SELECT * FROM mails").fetchone()
        assert row["notion_page_url"] == "" and row["notion_registered_at"] is None
        conn.close()


def test_page_url_only_trusts_notion():
    from server import notion
    pid = PAGE_ID
    assert notion.page_url({"url": "https://www.notion.so/Case-abc"}, pid) == \
        "https://www.notion.so/Case-abc"
    assert notion.page_url({"url": "https://app.notion.com/p/abc"}, pid) == \
        "https://app.notion.com/p/abc"
    for bad in ("javascript:alert(1)", "https://evil.example/notion.so", "http://www.notion.so/x",
                "https://www.notion.so/a b", ""):
        assert notion.page_url({"url": bad}, pid) == \
            "https://www.notion.so/" + pid.replace("-", "")


def test_mail_page_has_notion_ui(client):
    page = client.get("/mail").text
    for el in ('id="notion-card"', 'id="set-notion-token"', 'id="set-notion-token-clear"',
               'id="set-notion-db"', 'id="set-notion-map"', 'id="set-notion-ai"',
               'id="set-test-notion"', 'id="m-ntn-reg"', 'id="m-ntn-open"', 'id="m-ntn-again"'):
        assert el in page
    assert "notion.so/my-integrations" in page and "「接続」" in page
    assert 'rel="noopener noreferrer"' in page
    # シークレットは画面に出さず、入れたときだけ送る。消すのは専用のボタン
    assert 'if ($("set-notion-token").value.trim()) body.notion_token' in page
    assert 'JSON.stringify({ notion_token: "" })' in page
    # Notion から来た列の名前は esc() を通し、結果の文は textContent で入れる
    assert "${esc(p.name)}" in page and '$("m-ntn-msg").textContent = text' in page


def test_upload_name_matches_the_content_type(client, users, tmp_path, fake):
    """中身が PNG の『photo.jpg』や拡張子の無いファイルは、送る種類に合う拡張子で Notion に送る
    (Notion は種類とファイル名の拡張子が合わないと受け付けない)。ページの名前は元のまま."""
    from server import db, mail, notion
    assert notion._upload_name("photo.jpg", "image/png") == "photo.png"
    assert notion._upload_name("添付ファイル", "application/pdf") == "添付ファイル.pdf"
    assert notion._upload_name("plan.v2", "image/jpeg") == "plan.v2.jpg"
    assert notion._upload_name("a.JPG", "image/jpeg") == "a.JPG"
    assert notion._upload_name("見積書.pdf", "application/pdf") == "見積書.pdf"
    assert notion._upload_name("data", "application/octet-stream") == "data"
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_attachments(conn, tmp_path / "mail_attachments", mid, [
            {"filename": "photo.jpg", "content_type": "image/png", "size": len(PNG),
             "data": PNG, "skip": ""},
        ], "2026-10-22T00:00:00+00:00")
    d = client.post(f"/api/mail/{mid}/notion", headers=auth(admin)).json()
    assert d["attachments_uploaded"] == 1
    created = [c.body for c in fake.calls if c.path == "/file_uploads"]
    assert created == [{"mode": "single_part", "filename": "photo.png",
                        "content_type": "image/png"}]
    sends = [c for c in fake.calls if c.path.endswith("/send")]
    assert 'filename="photo.png"'.encode() in sends[0].body
    block = fake.of("PATCH", "/blocks/")[0].body["children"][0]
    assert block["image"]["caption"][0]["text"]["content"] == "photo.jpg"


@pytest.mark.parametrize("status", [500, 502, 504])
def test_page_5xx_asks_to_check_notion_before_pressing_again(client, users, tmp_path, fake,
                                                             status):
    """ページを作る依頼が 5xx のときは、できている場合があるので Notion で確かめるよう案内する."""
    _, admin = users
    _configure(client, admin)
    mid = _mail(tmp_path)
    fake.fail[("POST", "/pages")] = [(status, {"code": "gateway_timeout", "message": "x"}, None)]
    r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin))
    assert r.status_code == 502
    assert "登録できている場合があるので、Notionで確かめてから" in r.json()["detail"]
    assert _row(tmp_path, mid)["notion_page_url"] == ""
    # データベースを読む (GET) の 5xx は今までどおり (二重にはならない)
    fake.fail[("GET", "/databases/")] = [(503, {"code": "service_unavailable"}, None)]
    r = client.post(f"/api/mail/{mid}/notion", headers=auth(admin))
    assert "登録できている場合" not in r.json()["detail"]
