"""よく使う返信文 (定型文) のテスト.

設定画面で登録・並べ替えし、返信画面の「定型文を入れる」で本文に入れる。
{相手の名前} {件名} {日付} は、入れるときにそのメールの内容へ置き換える。
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

ROOT = Path(__file__).resolve().parent.parent


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


def names(client, admin):
    r = client.get("/api/mail/templates", headers=auth(admin))
    assert r.status_code == 200, r.text
    return [t["name"] for t in r.json()]


def add(client, admin, name, body="本文"):
    return client.post("/api/mail/templates", headers=auth(admin),
                       json={"name": name, "body": body})


# ---------- 初期値 (最初の1回だけ) ----------

DEFAULT_NAMES = ["チェックイン方法のご案内", "清掃のご依頼の受付", "お見積もりのお礼"]


def test_defaults_are_seeded_on_a_new_db(client, users):
    _, admin = users
    rows = client.get("/api/mail/templates", headers=auth(admin)).json()
    assert [t["name"] for t in rows] == DEFAULT_NAMES
    for t in rows:
        assert t["body"].startswith("{相手の名前} 様")
    # 差し込みを使った例になっている
    assert any("{件名}" in t["body"] for t in rows)
    assert any("{日付}" in t["body"] for t in rows)


def test_defaults_are_not_seeded_again_after_user_deletes_them(client, users, tmp_path):
    from server import db
    _, admin = users
    for t in client.get("/api/mail/templates", headers=auth(admin)).json():
        assert client.delete(f"/api/mail/templates/{t['id']}",
                             headers=auth(admin)).status_code == 200
    # 接続し直しても (再起動・次のリクエスト) 入れ直さない
    db.connect(tmp_path / "zatsumu.db").close()
    assert names(client, admin) == []


def test_seed_runs_on_upgrade_of_an_existing_db(tmp_path):
    """定型文の表が無かった頃の DB (本番) を開くと、表ができて初期値が1回だけ入る."""
    from server import db
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO users (name, token) VALUES ('x', 't')")  # 既存のデータ
    conn.execute("DROP TABLE mail_templates")
    conn.execute("DELETE FROM settings WHERE key = ?", (db.MAIL_TEMPLATES_SEEDED_KEY,))
    conn.commit()
    conn.close()

    for _ in range(2):   # 2回開いても二重に入らない
        conn = db.connect(path)
        rows = conn.execute(
            "SELECT name FROM mail_templates ORDER BY sort_order, id").fetchall()
        assert [r["name"] for r in rows] == DEFAULT_NAMES
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        # 開いた直後に書き込みを抱えたままにしない (この後の通信中にロックを持たない)
        assert not conn.in_transaction
        conn.close()


def test_seed_skips_a_table_that_already_has_templates(tmp_path):
    from server import db
    path = tmp_path / "z.db"
    conn = db.connect(path)
    conn.execute("DELETE FROM mail_templates")
    conn.execute("INSERT INTO mail_templates (name, body, created_at, updated_at)"
                 " VALUES ('自前', '本文', 'x', 'x')")
    conn.execute("DELETE FROM settings WHERE key = ?", (db.MAIL_TEMPLATES_SEEDED_KEY,))
    conn.commit()
    conn.close()
    conn = db.connect(path)
    assert [r["name"] for r in conn.execute("SELECT name FROM mail_templates")] == ["自前"]
    conn.close()


def test_seeded_flag_is_not_exposed_in_settings(client, users):
    _, admin = users
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert not any("templates" in k for k in s)


# ---------- 追加・変更・削除 ----------

def test_template_crud(client, users, tmp_path):
    from server import db
    worker, admin = users
    r = add(client, admin, "  お礼 \n の返信  ", "\r\n{相手の名前} 様\r\nありがとうございます。\r\n")
    assert r.status_code == 200, r.text
    t = r.json()
    # 名前は1行に、本文は改行を \n にそろえて前後の空白を落とす
    assert t["name"] == "お礼 の返信"
    assert t["body"] == "{相手の名前} 様\nありがとうございます。"
    # 一覧の一番下に入る
    assert names(client, admin) == DEFAULT_NAMES + ["お礼 の返信"]

    r = client.patch(f"/api/mail/templates/{t['id']}", headers=auth(admin),
                     json={"body": "変更後の本文"})
    assert r.status_code == 200 and r.json()["body"] == "変更後の本文"
    assert r.json()["name"] == "お礼 の返信"   # 送っていない項目は変わらない
    r = client.patch(f"/api/mail/templates/{t['id']}", headers=auth(admin),
                     json={"name": "お礼"})
    assert r.json()["name"] == "お礼" and r.json()["body"] == "変更後の本文"

    assert client.delete(f"/api/mail/templates/{t['id']}",
                         headers=auth(admin)).json() == {"deleted": t["id"]}
    assert "お礼" not in names(client, admin)
    assert client.delete(f"/api/mail/templates/{t['id']}",
                         headers=auth(admin)).status_code == 404
    assert client.patch(f"/api/mail/templates/{t['id']}", headers=auth(admin),
                        json={"name": "x"}).status_code == 404

    # 監査ログに残る
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
    assert {"mail_template_create", "mail_template_update",
            "mail_template_delete"} <= set(actions)


def test_template_validation(client, users, monkeypatch):
    from server import app as app_module
    _, admin = users
    assert add(client, admin, "  ", "本文").status_code == 400
    assert add(client, admin, "名前", " \n ").status_code == 400
    assert add(client, admin, "あ" * 41, "本文").status_code == 400
    assert add(client, admin, "あ" * 40, "本文").status_code == 200
    assert add(client, admin, "長い", "あ" * 5001).status_code == 400
    # 同じ名前は紛らわしいので断る (自分自身の名前のままの変更は通す)
    assert add(client, admin, "お見積もりのお礼").status_code == 409
    first = client.get("/api/mail/templates", headers=auth(admin)).json()[0]
    assert client.patch(f"/api/mail/templates/{first['id']}", headers=auth(admin),
                        json={"name": "お見積もりのお礼"}).status_code == 409
    assert client.patch(f"/api/mail/templates/{first['id']}", headers=auth(admin),
                        json={"name": first["name"], "body": "x"}).status_code == 200
    # 変更なし・不明な並べ替え・空の本文
    for body in ({}, {"move": "left"}, {"body": ""}):
        assert client.patch(f"/api/mail/templates/{first['id']}", headers=auth(admin),
                            json=body).status_code == 400, body
    # 件数の上限
    monkeypatch.setattr(app_module.mail, "TEMPLATE_MAX_COUNT", len(names(client, admin)))
    assert add(client, admin, "上限を超える").status_code == 400


def test_template_api_is_admin_only(client, users):
    worker, _ = users
    assert client.get("/api/mail/templates").status_code == 401
    assert client.get("/api/mail/templates", headers=auth(worker)).status_code == 403
    assert add(client, worker, "x").status_code == 403
    assert client.patch("/api/mail/templates/1", headers=auth(worker),
                        json={"name": "x"}).status_code == 403
    assert client.delete("/api/mail/templates/1",
                         headers=auth(worker)).status_code == 403
    assert client.get("/api/mail/1/templates", headers=auth(worker)).status_code == 403


# ---------- 並べ替え ----------

def test_template_move_up_and_down(client, users, tmp_path):
    from server import db
    _, admin = users
    ids = [t["id"] for t in client.get("/api/mail/templates", headers=auth(admin)).json()]
    a, b, c = ids

    def move(tid, where):
        r = client.patch(f"/api/mail/templates/{tid}", headers=auth(admin),
                         json={"move": where})
        assert r.status_code == 200, r.text

    def order():
        return [t["id"] for t in
                client.get("/api/mail/templates", headers=auth(admin)).json()]

    move(c, "up")
    assert order() == [a, c, b]
    move(a, "down")
    assert order() == [c, a, b]
    # 一番上の「上へ」・一番下の「下へ」は何もしない
    move(c, "up")
    move(b, "down")
    assert order() == [c, a, b]

    # 古いデータで並び順の番号が重なっていても、入れ替えで崩れない
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mail_templates SET sort_order = 0")
    assert order() == [a, b, c]          # 同じ番号なら作った順
    move(c, "up")
    assert order() == [a, c, b]
    # 名前の変更と並べ替えは1回で送っても両方効く
    r = client.patch(f"/api/mail/templates/{b}", headers=auth(admin),
                     json={"name": "改名", "move": "up"})
    assert r.json()["name"] == "改名"
    assert order() == [a, b, c]
    # 追加したものは一番下
    new = add(client, admin, "新しい定型文").json()["id"]
    assert order()[-1] == new


# ---------- /api/mail/{mail_id} に食われない ----------

def test_template_routes_are_not_shadowed_by_mail_id(client, users, tmp_path):
    from server import app as app_module
    _, admin = users
    mid = _add_mail(tmp_path)
    # "templates" が mail_id (整数) として読まれると 422 になる
    assert client.get("/api/mail/templates", headers=auth(admin)).status_code == 200
    assert client.get(f"/api/mail/{mid}", headers=auth(admin)).status_code == 200
    assert client.get(f"/api/mail/{mid}/templates",
                      headers=auth(admin)).status_code == 200

    # 宣言の順番そのものも確かめる (固定の道が {mail_id} より先)
    paths = [getattr(r, "path", "") for r in app_module.app.routes]
    first_param = paths.index("/api/mail/{mail_id}")
    for p in ("/api/mail/templates", "/api/mail/templates/{tid}"):
        assert all(i < first_param for i, x in enumerate(paths) if x == p), p


# ---------- 差し込みの置き換え ----------

def test_render_template_placeholders():
    from server import mail
    now = datetime(2026, 10, 21, 16, 30, tzinfo=timezone.utc)   # 日本時間では 22日 1:30
    m = {"from_name": "山田 太郎", "from_addr": "yamada@example.com",
         "subject": "清掃のご依頼（10/25）"}
    out = mail.render_template(
        "{相手の名前} 様\n「{件名}」の件、{日付}に承りました。{不明} {} {{件名}}", m, now)
    assert out == ("山田 太郎 様\n「清掃のご依頼（10/25）」の件、2026年10月22日に承りました。"
                   "{不明} {} {清掃のご依頼（10/25）}")
    # 名前が無ければアドレス、どちらも無ければ「ご担当者」。件名が無ければ「ご連絡」
    assert mail.render_template("{相手の名前}", {"from_name": "", "from_addr": "a@b.jp"},
                                now) == "a@b.jp"
    assert mail.render_template("{相手の名前}/{件名}", {}, now) == "ご担当者/ご連絡"
    # 日本語入力のままの全角の括弧でも置き換える
    assert mail.render_template("｛相手の名前｝様", m, now) == "山田 太郎様"
    # 件名に書かれた差し込みは二重に置き換えない・改行はまとめる
    evil = {"from_name": "悪意\r\nBcc: x@example.com", "subject": "{日付}{相手の名前}"}
    assert mail.render_template("{件名}|{相手の名前}", evil, now) == (
        "{日付}{相手の名前}|悪意 Bcc: x@example.com")
    # 日付は設定のタイムゾーンで数える
    from server import tz
    tz.set_tz("UTC")
    assert mail.render_template("{日付}", m, now) == "2026年10月21日"


def test_templates_rendered_for_a_mail(client, users, tmp_path):
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"mail_signature": "――\n株式会社テスト"})
    mid = _add_mail(tmp_path, from_name="<b>佐藤</b>", subject="鍵の受け渡し")
    add(client, admin, "確認", "{相手の名前} 様\n「{件名}」の件、確認します。")
    rows = client.get(f"/api/mail/{mid}/templates", headers=auth(admin)).json()
    assert [t["name"] for t in rows] == DEFAULT_NAMES + ["確認"]
    t = rows[-1]
    # 画面では textarea の value に入れるだけなので、HTML はそのまま (サーバで変えない)
    assert t["body"] == "<b>佐藤</b> 様\n「鍵の受け渡し」の件、確認します。"
    # 返信文が空のとき用は、「返信文を生成」と同じく署名付き
    assert t["body_with_signature"] == t["body"] + "\n\n――\n株式会社テスト"
    # 登録されている定型文そのものは置き換えない
    raw = client.get("/api/mail/templates", headers=auth(admin)).json()[-1]
    assert raw["body"].startswith("{相手の名前} 様")
    assert client.get("/api/mail/9999/templates", headers=auth(admin)).status_code == 404


def test_templates_work_without_ai_key(client, users, tmp_path, monkeypatch):
    """AI のキーが無くても定型文は使える (AI を呼ばない)."""
    from server import app as app_module
    _, admin = users
    monkeypatch.setattr(app_module.mail, "_call_claude",
                        lambda *a, **k: pytest.fail("AI を呼んではいけない"))
    mid = _add_mail(tmp_path)
    rows = client.get(f"/api/mail/{mid}/templates", headers=auth(admin)).json()
    assert rows[0]["body"].startswith("太郎 様")


# ---------- 画面 ----------

def test_mail_page_has_template_ui(client):
    page = client.get("/mail").text
    # 設定のカード・返信画面の選択欄
    for marker in ('id="tpl-card"', 'id="tpl-list"', 'id="tpl-name"', 'id="tpl-body"',
                   'id="tpl-save"', 'id="m-tpl"', 'data-ph="{相手の名前}"',
                   'data-ph="{件名}"', 'data-ph="{日付}"', '"/api/mail/templates"',
                   "`/api/mail/${id}/templates`"):
        assert marker in page, marker
    # 定型文の名前・本文 (利用者が入れた文字) は esc() か textContent で出す
    assert "${esc(t.name)}" in page
    assert "${esc(preview.slice(0, 60))}" in page
    assert "o.textContent = t.name" in page
    # 返信文へは textarea の value として入れる (innerHTML は使わない)
    assert "ta.value = t.body_with_signature" in page
    assert "insertAtCursor(ta, t.body)" in page
    # 送信後は選べない
    assert '$("m-tpl").disabled = true;' in page


# ---------- 定型文を続けて選んだとき (カーソルの位置の扱い) ----------
# 以前は返信文の欄の focus で「カーソルの位置を選んだ」としていたため、丸ごと入れた直後の
# ta.focus() でそうなってしまい、2つ目の定型文が「置き換えますか？」を聞かずに先頭へ入っていた

def _js_block(page: str, start: str, end: str) -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def test_template_cursor_only_from_user_actions(client):
    """カーソルの位置を選んだとみなすのは、人がタップ・キー操作したときだけ."""
    import re
    page = client.get("/mail").text
    events = set(re.findall(r'\$\("m-draft"\)\.addEventListener\("(\w+)"', page))
    assert {"click", "keydown", "input"} <= events
    # focus は ta.focus() でも、select は setSelectionRange でも起きる (人の操作ではない)。
    # keyup は確認を Enter で閉じたときのキーが届くことがある。pointerdown は iPhone で
    # 返信文の欄の上から画面を動かしただけでも起きる
    assert not events & {"focus", "select", "keyup", "pointerdown"}
    # Tab・Shift はカーソルを動かさない (Shift+Tab で定型文の欄に戻るとき)
    assert '"Tab", "Shift"' in page
    # 丸ごと入れ替えた後は、もう一度選んでもらう
    handler = _js_block(page, '$("m-tpl").onchange = () => {', "\n};\n")
    tail = handler[handler.index("ta.value = t.body_with_signature"):]
    assert tail.index("ta.setSelectionRange(0, 0);") < tail.index("cursorByProgram(ta);")
    # 入れ替えた後は欄の先頭 (宛名) を見せる (下までスクロールしたまま残ることがある)
    assert tail.index("ta.setSelectionRange(0, 0);") < tail.index("ta.scrollTop = 0;")


# ブラウザの代わりの最小限の部品。focus はその場で起き、select は後から起きる (Chrome と同じ)
_NODE_HARNESS = r"""
const listeners = {}, later = [];
const fire = (ev, e = {}) => (listeners[ev] || []).forEach(fn => fn(e));
const els = {
  "m-draft": {
    value: "", readOnly: false, selectionStart: 0, selectionEnd: 0,
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    focus() { fire("focus"); },
    setSelectionRange(s, e) { this.selectionStart = s; this.selectionEnd = e; later.push("select"); },
  },
  "m-tpl": { value: "" },
  "m-draft-src": { textContent: "" },
};
const $ = id => els[id];
let modal = { cursorSet: false, autoSel: null };
const modalTemplates = [
  { id: 1, name: "お礼", body: "A定型", body_with_signature: "A定型\n--署名" },
  { id: 2, name: "日程", body: "B定型", body_with_signature: "B定型\n--署名" },
];
const asked = [];
const confirm = msg => { asked.push(msg); return true; };
"""

_NODE_SCENARIO = r"""
const ta = $("m-draft");
const pick = id => {
  $("m-tpl").value = String(id);
  $("m-tpl").onchange();
  while (later.length) fire(later.shift());
};
const out = {};
// 1. 空の返信文に1つ目 → 2つ目
pick(1); pick(2);
out.second = { asked: asked.length, value: ta.value };
out.secondMsg = asked.join("");
// 2. 人が末尾をタップしてから選ぶ
asked.length = 0;
ta.selectionStart = ta.selectionEnd = ta.value.length;
fire("pointerdown"); fire("click"); pick(1);
out.tapped = { asked: asked.length, value: ta.value };
// 3. 空の欄をタップしてから1つ目 → 2つ目
asked.length = 0; ta.value = ""; modal.cursorSet = false;
fire("pointerdown"); fire("click"); pick(1); pick(2);
out.tapThenTwo = { asked: asked.length, value: ta.value };
// 4. iPhone: 1つ目の後、返信文の欄の上から指で画面を動かした (pointerdown だけ。
//    カーソルは先頭のまま) → 2つ目は聞く
asked.length = 0; ta.value = ""; modal.cursorSet = false;
pick(1); fire("pointerdown"); fire("pointercancel"); pick(2);
out.swipe = { asked: asked.length, value: ta.value };
// 5. PC: 1つ目の後、Shift+Tab で定型文の欄へ戻った → 2つ目は聞く
asked.length = 0; ta.value = ""; modal.cursorSet = false;
pick(1); fire("keydown", { key: "Shift" }); fire("keydown", { key: "Tab" }); pick(2);
out.shiftTab = { asked: asked.length, value: ta.value };
// 6. 1つ目の後、矢印キーでカーソルを動かした → 聞かずにその位置へ
asked.length = 0; ta.value = ""; modal.cursorSet = false;
pick(1); fire("keydown", { key: "End" }); ta.selectionStart = ta.selectionEnd = 3; pick(2);
out.arrow = { asked: asked.length, value: ta.value };
// 7. 1つ目の後、何のイベントも無くカーソルだけ動いた (長押しで選んだなど) → 聞かずに入れる
asked.length = 0; ta.value = ""; modal.cursorSet = false;
pick(1); ta.selectionStart = ta.selectionEnd = 3; pick(2);
out.moved = { asked: asked.length, value: ta.value };
console.log(JSON.stringify(out));
"""


def test_second_template_asks_before_replacing(client, tmp_path):
    """定型文を続けて選ぶと、2つ目は「置き換えますか？」と聞く (先頭に黙って入れない).

    画面の JavaScript (定型文を入れる部分) を node で動かす。node が無い環境では飛ばす。
    """
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    page = client.get("/mail").text
    block = _js_block(page, "// テキスト欄のカーソルの位置に文字を入れる",
                      "/* ---------- 設定 ---------- */")
    script = tmp_path / "tpl_check.js"
    script.write_text(_NODE_HARNESS + block + _NODE_SCENARIO, encoding="utf-8")
    r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    # 2つ目は聞いてから置き換える (1つ目の前に足さない)
    assert out["second"] == {"asked": 1, "value": "B定型\n--署名"}
    assert "定型文「日程」に置き換えますか" in out["secondMsg"]
    # 人が位置を選んだら、聞かずにその位置へ入れる
    assert out["tapped"] == {"asked": 0, "value": "B定型\n--署名A定型"}
    # 空の欄をタップしてから丸ごと入れても、次はまた聞く
    assert out["tapThenTwo"] == {"asked": 1, "value": "B定型\n--署名"}
    # 画面を指で動かしただけ・Shift+Tab で戻っただけなら、先頭に黙って入れない
    assert out["swipe"] == {"asked": 1, "value": "B定型\n--署名"}
    assert out["shiftTab"] == {"asked": 1, "value": "B定型\n--署名"}
    # カーソルを動かしたら、聞かずにその位置へ入れる
    assert out["arrow"] == {"asked": 0, "value": "A定型B定型\n--署名"}
    assert out["moved"] == {"asked": 0, "value": "A定型B定型\n--署名"}
