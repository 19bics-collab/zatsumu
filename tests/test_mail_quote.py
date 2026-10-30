"""メール本文の引用 (前のやり取り) を畳む表示のテスト.

- 画面の splitQuotes / bodyHtml を node で動かし、引用の見つけ方を確かめる
  (「>」の行・「〜 wrote:」「〜さんは書きました:」・Gmail / iPhone の日本語の書き方・
  「-----Original Message-----」・Outlook の「差出人: / 送信日時: / 宛先: / 件名:」)
- 畳まないもの: 引用が無い・短い引用 (途中に挟んだもの)・全部が引用・転送・HPフォーム
- 文字は esc() を通してから HTML にすること (本文の <script> などを動かさない)
- 返信画面・プルダウン・やり取り・取引先・送信箱の本文が、どれも bodyHtml を使うこと
"""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZATSUMU_LOGIN_VERIFY_EMAIL", raising=False)
    import importlib
    from server import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


def _run(page: str, scenario: str):
    """画面の esc と「引用:」の部分を node で動かし、scenario が出した JSON を返す."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    i = page.index("const esc = s =>")
    esc = page[i:page.index("\n", page.index("c =>", i)) + 1]
    block = page[page.index("// 引用: ここから"):page.index("// 引用: ここまで")]
    r = subprocess.run([node, "-e", esc + block + "\n" + scenario], capture_output=True,
                       text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _split(client, cases: dict) -> dict:
    """{名前: [本文, opts]} を splitQuotes にかけ、{名前: [[引用か, 文字], ...]} を返す."""
    out = _run(client.get("/mail").text,
               f"const C = {json.dumps(cases, ensure_ascii=False)};\n"
               "const o = {};\n"
               "for (const [k, [t, opts]] of Object.entries(C)) "
               "o[k] = splitQuotes(t, opts).map(g => [g.q, g.t]);\n"
               "console.log(JSON.stringify(o));")
    return {k: [tuple(g) for g in v] for k, v in out.items()}


QUOTE = "\n".join(f"> 前のメールの{i}行目" for i in range(1, 6))


def test_quote_styles_are_folded(client):
    out = _split(client, {
        # Gmail (日本語): 「2026年10月27日(月) 10:00 名前 <アドレス>:」と「>」の行
        "gmail_jp": ["承知しました。\n\n黒須\n\n2026年10月27日(月) 10:00 山田太郎 <taro@example.com>:\n\n"
                     + QUOTE + "\n>\n> 山田", {}],
        # Gmail (英語) が名前の途中で2行に折り返したもの
        "gmail_en": ["OK\n\nOn Mon, Oct 27, 2026 at 10:00 AM Taro Yamada <\ntaro@example.com> wrote:\n"
                     + QUOTE, {}],
        # iPhone のメール (日本語)
        "iphone": ["了解です\n\n2026/10/27 10:00、山田 太郎 <taro@example.com>のメール:\n\n" + QUOTE, {}],
        # Becky! など「〜さんは書きました:」・全角の「＞」
        "becky": ["はい\n\n山田さんは書きました:\n" + QUOTE.replace(">", "＞"), {}],
        # 改行が CRLF
        "crlf": ["了解\r\n\r\n" + QUOTE.replace("\n", "\r\n"), {}],
    })
    assert out["gmail_jp"] == [
        (False, "承知しました。\n\n黒須"),
        (True, "2026年10月27日(月) 10:00 山田太郎 <taro@example.com>:\n\n" + QUOTE + "\n>\n> 山田")]
    assert out["gmail_en"] == [
        (False, "OK"),
        (True, "On Mon, Oct 27, 2026 at 10:00 AM Taro Yamada <\ntaro@example.com> wrote:\n" + QUOTE)]
    assert out["iphone"] == [
        (False, "了解です"), (True, "2026/10/27 10:00、山田 太郎 <taro@example.com>のメール:\n\n" + QUOTE)]
    assert out["becky"] == [(False, "はい"), (True, "山田さんは書きました:\n" + QUOTE.replace(">", "＞"))]
    assert out["crlf"] == [(False, "了解"), (True, QUOTE)]


def test_whole_previous_message_is_folded(client):
    outlook = ("差出人: インビックス <info@example.com>\n送信日時: 2026年10月20日 9:00\n"
               "宛先: 鈴木 <suzuki@example.com>\n件名: RE: 清掃\n\n鈴木様\n前のメールの本文")
    out = _split(client, {
        "orig": ["了解です\n\n-----Original Message-----\nFrom: a@example.com\nSent: x\n\n前の本文", {}],
        "orig_jp": ["了解です\n\n----- 元のメッセージ -----\n前の本文", {}],
        # Outlook: 見出しの上の線から下を全部
        "outlook": ["鈴木様\n\nありがとうございます。\n\n________________________________\n" + outlook, {}],
        "outlook_en": ["Thanks\n\nFrom: Taro <t@example.com>\nSent: Monday, October 27, 2026 10:00 AM\n"
                       "To: info@example.com\nSubject: RE: x\n\nold", {}],
        # 見出しの前の「>」の引用も畳む (別々に)
        "both": ["はい\n\n" + QUOTE + "\n\nいいえ\n\n-----Original Message-----\n前", {}],
    })
    assert out["orig"] == [(False, "了解です"),
                           (True, "-----Original Message-----\nFrom: a@example.com\nSent: x\n\n前の本文")]
    assert out["orig_jp"] == [(False, "了解です"), (True, "----- 元のメッセージ -----\n前の本文")]
    assert out["outlook"] == [(False, "鈴木様\n\nありがとうございます。"),
                              (True, "________________________________\n" + outlook)]
    assert out["outlook_en"][0] == (False, "Thanks") and out["outlook_en"][1][1].startswith("From: Taro")
    assert out["both"] == [(False, "はい"), (True, QUOTE), (False, "いいえ"),
                           (True, "-----Original Message-----\n前")]


def test_what_is_not_folded(client):
    plain = "ふつうのメールです。\n引用はありません。"
    inline = "> 質問1ですが\nはい、大丈夫です。\n> 質問2\n> その2\nいいえ。"
    fwd = ("見てください\n\n---------- Forwarded message ---------\nFrom: X <x@example.com>\n"
           "Date: 2026年10月27日\nSubject: 予約\nTo: me@example.com\n\n予約の中身\n" + QUOTE)
    outlook_fwd = ("見てください\n\n________________________________\nFrom: X\nSent: 2026\nTo: me\n"
                   "Subject: 予約\n\n中身")
    # 本文の一番上から見出しの形 (前に書いた文が無い): 畳むと何も残らない
    top = "From: a@example.com\nDate: 2026\nSubject: x\n\n本文"
    cases = {
        "plain": [plain, {}],
        "inline": [inline, {}],                         # 途中に挟んだ短い引用
        "all": [QUOTE, {}],                              # 全部が引用
        "fwd": [fwd, {}],                                # 転送 (印から下は転送した中身)
        "fwd_subject": [outlook_fwd, {"subject": "FW: 予約"}],   # Outlook の転送は件名で分かる
        "fwd_jp_subject": [outlook_fwd, {"subject": "転送: 予約"}],
        "form": ["お名前: 山田\n\n" + QUOTE, {"form": True}],    # HPフォームから届いた
        "top": [top, {}],
        "empty": ["", {}],
    }
    out = _split(client, cases)
    for name, (text, _opts) in cases.items():
        assert out[name] == [(False, text)], name
    # 「Re: Fw:」(転送への返信) は畳む
    assert len(_split(client, {"re": [outlook_fwd, {"subject": "Re: Fw: 予約"}]})["re"]) == 2


def test_long_single_line_quote_is_folded(client):
    long_line = "> " + "あ" * 320
    out = _split(client, {"long": ["はい\n\n" + long_line, {}], "short": ["はい\n\n> " + "あ" * 50, {}]})
    assert out["long"] == [(False, "はい"), (True, long_line)]
    assert out["short"] == [(False, "はい\n\n> " + "あ" * 50)]


def test_body_html_escapes_text_and_toggles(client):
    page = client.get("/mail").text
    out = _run(page, r"""
const t = "hi <b>x</b> & 'q'\n\n> <script>alert(1)</script>\n> 2\n> 3\n> 4";
quoteOpen.add("k:1");
console.log(JSON.stringify({
  closed: bodyHtml(t), open: bodyHtml(t, { key: "k" }), plain: bodyHtml("<img src=x onerror=alert(1)>"),
  empty: bodyHtml(""), nul: bodyHtml(null),
}));""")
    assert out["closed"] == (
        "hi &lt;b&gt;x&lt;/b&gt; &amp; &#39;q&#39;"
        '<button type="button" class="ghost q-tg" aria-expanded="false" data-lines="4">'
        "▼ 引用を表示（前のやり取り・4行）</button>"
        '<div class="q-body" hidden>&gt; &lt;script&gt;alert(1)&lt;/script&gt;\n&gt; 2\n&gt; 3\n&gt; 4'
        '<button type="button" class="ghost q-end">▲ 引用を隠す</button></div>')
    # key を付けたものは、開いたことを覚えていれば開いたまま描く
    assert 'aria-expanded="true" data-lines="4" data-qk="k:1">▲ 引用を隠す</button><div class="q-body">' in out["open"]
    assert out["plain"] == "&lt;img src=x onerror=alert(1)&gt;"
    assert out["empty"] == out["nul"] == "（本文なし）"


def test_every_mail_body_uses_body_html(client):
    page = client.get("/mail").text
    for use in (
        # 返信画面・やり取り (返信画面の下)・取引先のやり取り・送信箱
        '$("m-body").innerHTML = bodyHtml(m.body, { subject: m.subject, form: m.via_form });',
        "el.innerHTML = bodyHtml(d.body, { subject: d.subject, form: d.via_form });",
        "${bodyHtml(t.reply_body)}",
        "tlBodies.set(`in:${Number(idText)}`, bodyHtml(m.body, { subject: m.subject, form: m.via_form }));",
        '$("sd-body").innerHTML = bodyHtml(d.body, { subject: d.subject });',
        # 一覧のプルダウン (描き直しても、開いた引用は開いたまま)
        'bodyHtml(e.body, { key: "pko:" + Number(e.mail_id) })',
        'bodyHtml(e.body, { subject: e.subject, form: e.via_form, key: "pk:" + Number(e.mail_id) })',
        'bodyHtml(d.body, { subject: d.subject ?? m.subject, form: d.via_form, key: "pk:" + Number(id) })',
    ):
        assert use in page, use
    # 本文を textContent や esc() でそのまま入れている所は残っていない
    for old in ('textContent = m.body', 'textContent = d.body', "${esc(e.body)", "${esc(d.body)",
                "${esc(t.reply_body)", 'tlBodies.set(`in:${Number(idText)}`, m.body'):
        assert old not in page, old
    # 押したときの動き: 開く・閉じる・下の「引用を隠す」で閉じたら上のボタンを見せる
    i = page.index("// 「引用を表示 ▼」「引用を隠す ▲」")
    h = page[i:page.index("\n});", i)]
    assert 'tg.setAttribute("aria-expanded", String(open));' in h and "box.hidden = !open;" in h
    assert 'tg.scrollIntoView({ block: "center" })' in h
    assert "quoteOpen.add(k)" in h and "quoteOpen.delete(k)" in h
    # 引用は本文の中で折り返し、中だけのスクロール枠にしない
    css = page[page.index("  .mail-body .q-tg {"):page.index("  #m-draft {")]
    assert "max-height" not in css and "overflow: auto" not in css
    assert ".mail-body .q-body[hidden] { display: none; }" in css
    # 使い方に書く
    assert "「引用を表示 ▼」を押してください" in page
