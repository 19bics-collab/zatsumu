"""AI (Claude API) を使えるようにする整備のテスト.

ネットワークには出ない。anthropic.Anthropic を偽物に差し替え、送った依頼の中身と、
失敗したときにルール判定・定型文へ切り替わって理由が画面に届くことを確かめる。
SDK の例外クラスは本物を使う (種類ごとの日本語の説明を確かめるため)。
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

try:  # SDK 1.x 以降は httpx2 の上に作られている
    import httpx2 as httpx
except ImportError:  # pragma: no cover  古い SDK (httpx 版)
    import httpx

KEY = "sk-ant-test-key-123"
MAIL = {"from_addr": "taro@example.com", "from_name": "太郎",
        "subject": "清掃日程の件", "body": "来週の清掃をお願いできますか"}


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


@pytest.fixture(autouse=True)
def reset_ai_errors():
    # AI の失敗の記録はプロセス全体の状態なので、テストごとに消す
    from server import mail
    mail.clear_ai_errors()
    yield
    mail.clear_ai_errors()


def _resp(text="", stop_reason="end_turn"):
    content = [SimpleNamespace(type="thinking", thinking="")]  # 考える過程の部品も混ざる
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class FakeAnthropic:
    """anthropic.Anthropic の偽物。作られ方と messages.create の引数を記録する."""
    instances: list = []
    calls: list = []
    reply = None   # 返す応答 (関数なら引数を受けて応答を返すか例外を投げる)

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        FakeAnthropic.instances.append(self)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **params):
        FakeAnthropic.calls.append(params)
        r = FakeAnthropic.reply
        return r(params) if callable(r) else r

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


@pytest.fixture()
def fake_ai(monkeypatch):
    from server import mail
    FakeAnthropic.instances = []
    FakeAnthropic.calls = []
    FakeAnthropic.reply = _resp("{}")
    monkeypatch.setattr(mail.anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


def _req():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status, message):
    import anthropic
    resp = httpx.Response(status, request=_req())
    return getattr(anthropic, cls)(message, response=resp, body=None)


def _settings(**kw):
    from server import db
    return {**db.INT_SETTINGS, **db.STR_SETTINGS, "anthropic_api_key": KEY,
            "company_name": "インビックス", **kw}


# ---------- 依頼の中身 ----------

def test_defaults_are_current_models():
    """既定のモデルは今使えるもの。判定は安いモデル、返信文は品質重視のモデル."""
    from server import db, mail
    assert db.STR_SETTINGS["anthropic_model"] == "claude-opus-5-5"
    assert db.STR_SETTINGS["anthropic_classify_model"] == "claude-haiku-5-5"
    # 設定が空なら既定値を使う
    s = _settings(anthropic_model="", anthropic_classify_model=" ")
    assert mail.ai_model(s) == "claude-opus-5-5"
    assert mail.ai_classify_model(s) == "claude-haiku-5-5"


def test_classify_uses_cheap_model_with_short_timeout(fake_ai):
    from server import mail
    fake_ai.reply = _resp('判定: {"priority": 1, "reason": "クレーム"}')
    p, reason, source = mail.classify_priority(_settings(), MAIL)
    assert (p, reason, source) == (1, "クレーム", "ai")
    (inst,) = fake_ai.instances
    # キーは前後の空白を除いて渡す・待ち時間とやり直し回数を区切る・使い終えたら閉じる
    assert inst.kwargs == {"api_key": KEY, "timeout": mail.AI_CLASSIFY_TIMEOUT,
                           "max_retries": mail.AI_CLASSIFY_RETRIES}
    assert inst.closed
    (params,) = fake_ai.calls
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"] == {"effort": "low"}
    assert params["max_tokens"] == mail.AI_CLASSIFY_MAX_TOKENS
    assert params["messages"][0]["role"] == "user"
    assert "清掃日程の件" in params["messages"][0]["content"]
    # 今の API で断られる指定 (温度・思考を切る・考える量の固定) は送らない
    for gone in ("temperature", "top_p", "top_k", "thinking"):
        assert gone not in params, gone
    assert mail.ai_errors() == []


def test_generate_reply_uses_main_model(fake_ai):
    from server import mail
    fake_ai.reply = _resp("太郎 様\n\nご連絡ありがとうございます。")
    body, source = mail.generate_reply(
        _settings(mail_signature="――\nインビックス"), MAIL, "日程を確認すると伝える")
    assert source == "ai"
    assert body.startswith("太郎 様") and body.endswith("インビックス")
    (inst,) = fake_ai.instances
    assert inst.kwargs["timeout"] == mail.AI_DRAFT_TIMEOUT
    assert inst.kwargs["max_retries"] == mail.AI_DRAFT_RETRIES
    (params,) = fake_ai.calls
    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"] == {"effort": "medium"}
    assert params["max_tokens"] == mail.AI_DRAFT_MAX_TOKENS
    assert "インビックス" in params["system"]
    assert "日程を確認すると伝える" in params["messages"][0]["content"]


def test_saved_old_default_model_keeps_working(fake_ai, client, users, tmp_path):
    """以前の既定値 (保存済み) はそのまま使い続けられる (勝手に書き換えない)."""
    from server import db, mail
    _, admin = users
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"anthropic_model": "claude-opus-5"})
    assert r.status_code == 200 and r.json()["anthropic_model"] == "claude-opus-5"
    # 新しく増えた判定用のモデルは、保存が無くても既定値で動く
    assert r.json()["anthropic_classify_model"] == "claude-haiku-5-5"
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "anthropic_api_key": KEY}
    fake_ai.reply = _resp("太郎 様\n\n承知しました。")
    assert mail.generate_reply(s, MAIL)[1] == "ai"
    assert fake_ai.calls[0]["model"] == "claude-opus-5"
    assert fake_ai.calls[0]["output_config"] == {"effort": "medium"}


def test_effort_is_not_sent_to_older_models(fake_ai):
    """考える深さ (effort) を受け付けない古い世代には付けない (付けると依頼ごと断られる)."""
    from server import mail
    fake_ai.reply = _resp('{"priority": 3, "reason": "お知らせ"}')
    mail.classify_priority(_settings(anthropic_classify_model="claude-haiku-4-5"), MAIL)
    assert "output_config" not in fake_ai.calls[0]
    assert fake_ai.calls[0]["model"] == "claude-haiku-4-5"


def test_no_api_call_without_key(fake_ai):
    from server import mail
    s = _settings(anthropic_api_key="  ", mail_urgent_keywords="至急")
    assert mail.classify_priority(s, {**MAIL, "subject": "至急"})[2] == "rule"
    assert mail.generate_reply(s, MAIL)[1] == "template"
    assert fake_ai.instances == [] and mail.ai_errors() == []


# ---------- 失敗したときの切り替えと理由 ----------

def test_unknown_model_falls_back_with_clear_reason(fake_ai):
    """保存されたモデル名が古い・間違っている → ルール判定と定型文に切り替え、理由を残す."""
    from server import mail

    def not_found(params):
        raise _status_error("NotFoundError", 404, f"model: {params['model']}")

    fake_ai.reply = not_found
    s = _settings(anthropic_model="claude-3-opus-20240229",
                  anthropic_classify_model="claude-typo",
                  mail_urgent_keywords="至急")
    p, _reason, source = mail.classify_priority(s, {**MAIL, "subject": "至急"})
    assert (p, source) == (1, "rule")
    body, source = mail.generate_reply(s, MAIL)
    assert source == "template" and "拝受いたしました" in body
    errs = {e["purpose"]: e for e in mail.ai_errors()}
    assert set(errs) == {"draft", "classify"}
    assert "claude-3-opus-20240229" in errs["draft"]["message"]
    assert "見つかりません" in errs["draft"]["message"]
    assert "選び直して" in errs["draft"]["message"]
    assert errs["classify"]["model"] == "claude-typo"
    assert errs["classify"]["label"] == "優先度の判定" and errs["classify"]["at"]
    assert mail.last_ai_error("draft") == errs["draft"]["message"]

    # 同じ用途で成功したら、その用途の記録だけ消える
    fake_ai.reply = _resp("太郎 様\n\n承知しました。")
    assert mail.generate_reply(s, MAIL)[1] == "ai"
    assert [e["purpose"] for e in mail.ai_errors()] == ["classify"]


@pytest.mark.parametrize("stop_reason,text,expect", [
    ("refusal", "", "断りました"),
    ("max_tokens", "太郎 様\n\nご連絡", "途中で切れました"),
    ("end_turn", "", "文章が返ってきませんでした"),
])
def test_unusable_answers_fall_back_to_template(fake_ai, stop_reason, text, expect):
    """断られた・途中で切れた・空の返事は、そのまま下書きにせず定型文にする."""
    from server import mail
    fake_ai.reply = _resp(text, stop_reason)
    body, source = mail.generate_reply(_settings(), MAIL)
    assert source == "template" and "拝受いたしました" in body
    assert expect in mail.last_ai_error("draft")


def test_unreadable_classification_falls_back_to_rule(fake_ai):
    from server import mail
    fake_ai.reply = _resp('{"priority": 9}')
    assert mail.classify_priority(_settings(), MAIL)[2] == "rule"
    assert "読み取れません" in mail.last_ai_error("classify")
    fake_ai.reply = _resp("高です")
    assert mail.classify_priority(_settings(), MAIL)[2] == "rule"


def test_error_texts_are_plain_japanese():
    """SDK の例外の種類ごとに、次に何をすればよいか分かる日本語にする."""
    import anthropic
    from server import mail
    m = "claude-opus-5-5"
    cases = [
        (_status_error("AuthenticationError", 401, "invalid x-api-key"),
         "APIキーが正しくありません"),
        (_status_error("PermissionDeniedError", 403, "forbidden"), "権限がありません"),
        (_status_error("NotFoundError", 404, "model: x"), "見つかりません"),
        (_status_error("RateLimitError", 429, "rate limited"), "上限に達しました"),
        (_status_error("BadRequestError", 400,
                       "Your credit balance is too low to access the Anthropic API."),
         "残高"),
        (_status_error("BadRequestError", 400, "max_tokens: too large"),
         "受け付けられませんでした"),
        (_status_error("APIStatusError", 529, "Overloaded"), "混み合っている"),
        (_status_error("InternalServerError", 500, "boom"), "混み合っている"),
        (anthropic.APITimeoutError(request=_req()), "時間内に届きませんでした"),
        (anthropic.APIConnectionError(request=_req()), "つながりませんでした"),
    ]
    for exc, expect in cases:
        assert expect in mail._ai_error_text(exc, m), (type(exc), expect)
    assert "max_tokens: too large" in mail._ai_error_text(cases[5][0], m)


def _fake_inbox(monkeypatch, n):
    """n 通入った受信箱 (IMAP) の偽物を差し込む."""
    from email.message import EmailMessage
    from server import mail

    def raw(uid):
        m = EmailMessage()
        m["From"] = f"guest{uid}@example.com"
        m["Subject"] = f"問い合わせ{uid}"
        m["Message-ID"] = f"<u{uid}@example.com>"
        m.set_content("本文")
        return m.as_bytes()

    class Box:
        def select(self, folder, readonly=False):
            return ("OK", [str(n).encode()])

        def response(self, key):
            return ("UIDVALIDITY", [b"1"])

        def uid(self, cmd, *args):
            if cmd == "search":
                return ("OK", [b" ".join(str(u).encode() for u in range(1, n + 1))])
            return ("OK", [(b"header", raw(int(args[0])))])

        def logout(self):
            pass

    monkeypatch.setattr(mail, "_imap_connect", lambda s: Box())


def test_fetch_stops_using_ai_after_repeated_failures(fake_ai, monkeypatch, tmp_path):
    """AI が続けて失敗したら、その回の残りのメールは待たずにルール判定・定型文にする."""
    import anthropic
    from server import db, mail
    _fake_inbox(monkeypatch, 6)

    def timeout(params):
        raise anthropic.APITimeoutError(request=_req())

    fake_ai.reply = timeout
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "anthropic_api_key": KEY, "mail_auto_draft": 1}
        added = mail.fetch_new_mail(conn, s)
        assert len(added) == 6        # メール自体は全部取り込む
        rows = conn.execute(
            "SELECT priority_source, draft_source FROM mails").fetchall()
    assert {(r["priority_source"], r["draft_source"]) for r in rows} == {
        ("rule", "template")}
    # 判定・下書きを3通分試したところで打ち切り (残り3通は AI を呼ばない)
    assert len(fake_ai.calls) == mail.AI_MAX_CONSECUTIVE_FAILURES * 2
    # 理由は画面に出せるよう残っている
    assert "時間内に届きませんでした" in mail.last_ai_error("classify")


def test_fetch_keeps_using_ai_when_it_works(fake_ai, monkeypatch, tmp_path):
    from server import db, mail
    _fake_inbox(monkeypatch, 5)

    def reply(params):
        if params["max_tokens"] == mail.AI_CLASSIFY_MAX_TOKENS:
            return _resp('{"priority": 2, "reason": "通常の問い合わせ"}')
        return _resp("ご担当者 様\n\n承知しました。")

    fake_ai.reply = reply
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "anthropic_api_key": KEY, "mail_auto_draft": 1}
        mail.fetch_new_mail(conn, s)
        rows = conn.execute(
            "SELECT priority_source, draft_source FROM mails").fetchall()
    assert {(r["priority_source"], r["draft_source"]) for r in rows} == {("ai", "ai")}
    assert len(fake_ai.calls) == 10
    # 判定は安いモデル、下書きは品質重視のモデル
    assert {c["model"] for c in fake_ai.calls} == {"claude-haiku-5-5", "claude-opus-5-5"}


def test_without_sdk_reports_clear_reason(monkeypatch):
    from server import mail
    monkeypatch.setattr(mail, "anthropic", None)
    with pytest.raises(mail.AIError, match="anthropic"):
        mail.test_ai(_settings())
    # キーがあっても SDK が無ければ AI は使わない (ルール判定のまま動く)
    assert mail.classify_priority(_settings(), MAIL)[2] == "rule"


# ---------- AI 接続テスト (POST /api/settings/test-ai) ----------

def test_test_ai_endpoint(fake_ai, client, users):
    from server import mail
    worker, admin = users
    # キー未設定 → 400 (保存を促す)
    r = client.post("/api/settings/test-ai", headers=auth(admin))
    assert r.status_code == 400 and "保存" in r.json()["detail"]
    assert fake_ai.calls == []
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY})
    fake_ai.reply = _resp("OK")
    r = client.post("/api/settings/test-ai", headers=auth(admin))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "claude-opus-5-5",
                        "classify_model": "claude-haiku-5-5"}
    # 返信文用・判定用の両方のモデルを、短い依頼・やり直し無しで1回ずつ試す
    assert [c["model"] for c in fake_ai.calls] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert all(c["max_tokens"] == mail.AI_TEST_MAX_TOKENS for c in fake_ai.calls)
    assert all(i.kwargs["max_retries"] == 0 for i in fake_ai.instances)
    # 一般ユーザーは使えない
    assert client.post("/api/settings/test-ai",
                       headers=auth(worker)).status_code == 403


def test_test_ai_same_model_is_tried_once(fake_ai, client, users):
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY,
                       "anthropic_classify_model": "claude-opus-5-5"})
    fake_ai.reply = _resp("OK")
    assert client.post("/api/settings/test-ai", headers=auth(admin)).status_code == 200
    assert len(fake_ai.calls) == 1


def test_test_ai_reports_failure_and_masks_key(fake_ai, client, users):
    """失敗は 502 と理由。キーが例外文に混ざっても画面には出さない."""
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY})

    def bad_key(params):
        raise _status_error("BadRequestError", 400, f"bad header x-api-key: {KEY}")

    fake_ai.reply = bad_key
    r = client.post("/api/settings/test-ai", headers=auth(admin))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert KEY not in detail and "***" in detail
    assert "受け付けられませんでした" in detail
    # 失敗は設定画面にも出る (キーは伏せたまま)
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert {e["purpose"] for e in s["ai_errors"]} == {"draft", "classify"}
    assert all(KEY not in e["message"] for e in s["ai_errors"])
    assert s["anthropic_api_key"] == "" and s["anthropic_api_key_set"] is True


def test_test_ai_names_the_failing_model(fake_ai, client, users):
    """片方のモデルだけ使えないときは、どちらの用途のどのモデルかを書く."""
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY,
                       "anthropic_classify_model": "claude-typo"})

    def reply(params):
        if params["model"] == "claude-typo":
            raise _status_error("NotFoundError", 404, "model: claude-typo")
        return _resp("OK")

    fake_ai.reply = reply
    r = client.post("/api/settings/test-ai", headers=auth(admin))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "優先度の判定（claude-typo）" in detail and "見つかりません" in detail
    assert "返信文の作成" not in detail
    errs = client.get("/api/settings", headers=auth(admin)).json()["ai_errors"]
    assert [e["purpose"] for e in errs] == ["classify"]

    # 直して保存すると古い記録は消え、テストが通れば何も出ない
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_classify_model": "claude-haiku-5-5"})
    assert client.get("/api/settings", headers=auth(admin)).json()["ai_errors"] == []
    assert client.post("/api/settings/test-ai", headers=auth(admin)).status_code == 200


# ---------- 設定の保存 ----------

def test_model_settings_validation(client, users, tmp_path):
    from server import db, mail
    _, admin = users
    for bad in ("claude opus", "claude-opus\nX", "<script>", "x" * 101):
        r = client.patch("/api/settings", headers=auth(admin),
                         json={"anthropic_classify_model": bad})
        assert r.status_code == 400, bad
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"anthropic_model": " claude-sonnet-5-5 ",
                           "anthropic_classify_model": ""})
    assert r.status_code == 200
    assert r.json()["anthropic_model"] == "claude-sonnet-5-5"   # 前後の空白は除く
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = db.get_settings(conn)
    assert mail.ai_classify_model(s) == "claude-haiku-5-5"      # 空なら既定値


def test_unrelated_save_keeps_ai_errors(client, users):
    """キーやモデルを変えない保存では、AI の失敗の記録を消さない (原因は残っているため)."""
    from server import mail
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY})
    mail._record_ai_error("classify", "claude-haiku-5-5", "残高が足りません")
    current = client.get("/api/settings", headers=auth(admin)).json()
    client.patch("/api/settings", headers=auth(admin),
                 json={"mail_signature": "署名",
                       "anthropic_model": current["anthropic_model"],
                       "anthropic_classify_model": current["anthropic_classify_model"]})
    errs = client.get("/api/settings", headers=auth(admin)).json()["ai_errors"]
    assert [e["message"] for e in errs] == ["残高が足りません"]


def test_ai_errors_hidden_without_key(client, users):
    from server import mail
    _, admin = users
    mail._record_ai_error("draft", "claude-opus-5-5", "古い記録")
    assert client.get("/api/settings", headers=auth(admin)).json()["ai_errors"] == []


# ---------- 下書きの生成 (画面) ----------

def test_draft_endpoint_reports_ai_failure(fake_ai, client, users, tmp_path):
    _, admin = users
    mid = _add_mail(tmp_path)
    client.patch("/api/settings", headers=auth(admin),
                 json={"anthropic_api_key": KEY})

    def no_credit(params):
        raise _status_error("BadRequestError", 400,
                            "Your credit balance is too low to access the Anthropic API.")

    fake_ai.reply = no_credit
    r = client.post(f"/api/mail/{mid}/draft", headers=auth(admin),
                    json={"instructions": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["draft_source"] == "template" and body["draft_reply"]
    assert "残高" in body["ai_error"]
    # AI で作れたときは理由は空
    fake_ai.reply = _resp("太郎 様\n\n承知しました。")
    body = client.post(f"/api/mail/{mid}/draft", headers=auth(admin),
                       json={"instructions": ""}).json()
    assert body["draft_source"] == "ai" and body["ai_error"] == ""


# ---------- 画面 ----------

def test_mail_page_ai_settings(client):
    """AI 設定のカード: モデルは選択式 (既定はサーバと同じ)・接続テスト・キーの案内."""
    import re
    from server import db
    page = client.get("/mail").text
    for marker in ('id="set-anthropic-model"', 'id="set-anthropic-classify-model"',
                   'id="set-test-ai"', 'id="test-ai-msg"', 'id="ai-status"',
                   'id="ai-warn"', 'id="m-ai-error"', '"/api/settings/test-ai"',
                   "anthropic_classify_model:"):
        assert marker in page, marker
    # モデルは打ち込まずに選ぶ
    assert '<select id="set-anthropic-model">' in page
    # 画面の「おすすめ」はサーバの既定値と一致させる
    assert f'AI_DEFAULT_MODEL = "{db.STR_SETTINGS["anthropic_model"]}"' in page
    assert (f'AI_DEFAULT_CLASSIFY_MODEL = "{db.STR_SETTINGS["anthropic_classify_model"]}"'
            in page)
    for model in re.findall(r'\["(claude-[^"]+)", "', page):
        assert model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
    # キーの取り方・料金・チャットに貼らない注意
    assert "console.anthropic.com" in page
    assert "料金" in page and "貼り付けたり" in page
    # 外から来た文字 (モデル名・失敗の理由) は innerHTML に入れない
    assert "o.textContent = label" in page
    assert '$("ai-status").textContent' in page
