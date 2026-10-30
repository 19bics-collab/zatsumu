"""メール本文の引用 (前のやり取り) を畳む表示のテスト.

- 画面の splitQuotes / bodyHtml を node で動かし、引用の見つけ方を確かめる
  (「>」の行・「〜 wrote:」「〜さんは書きました:」・Gmail / iPhone の日本語の書き方・
  「-----Original Message-----」・Outlook の「差出人: / 送信日時: / 宛先: / 件名:」)
- 相手が新しく書いた所は畳まないこと: フォーム・予約の知らせの「差出人:」「日時:」の項目、
  文の中の「From: / To: / Date:」、「〜 wrote:」のすぐ上の文、「書きました」で終わる文、
  「＞」の箇条書き、引用と答えを交互に書いたメールの質問の引用
- 畳まないもの: 引用が無い・短い引用 (途中に挟んだもの)・全部が引用・転送・HPフォーム
- とても長い本文でも、すぐに分けられること
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
        # Gmail (英語) が日時の後で折り返したもの・Gmail (日本語) が名前の後で折り返したもの
        "gmail_en2": ["OK\n\nOn Mon, Oct 27, 2026 at 10:00 AM\nTaro Yamada <taro@example.com> wrote:\n"
                      + QUOTE, {}],
        "gmail_jp2": ["承知しました。\n\n2026年10月27日(月) 10:00 山田太郎\n<taro@example.com>:\n\n"
                      + QUOTE, {}],
        # スマホの返信: 誰が書いたかの行が無く、本文の一番下 (空行の後) に「>」の引用
        "mobile": ["了解です。明日伺います。\n\n" + QUOTE + "\n\n", {}],
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
    assert out["gmail_en2"] == [
        (False, "OK"),
        (True, "On Mon, Oct 27, 2026 at 10:00 AM\nTaro Yamada <taro@example.com> wrote:\n" + QUOTE)]
    assert out["gmail_jp2"] == [
        (False, "承知しました。"),
        (True, "2026年10月27日(月) 10:00 山田太郎\n<taro@example.com>:\n\n" + QUOTE)]
    assert out["mobile"] == [(False, "了解です。明日伺います。"), (True, QUOTE)]


def test_whole_previous_message_is_folded(client):
    outlook = ("差出人: インビックス <info@example.com>\n送信日時: 2026年10月20日 9:00\n"
               "宛先: 鈴木 <suzuki@example.com>\n件名: RE: 清掃\n\n鈴木様\n前のメールの本文")
    # 社内の人からのメール (Outlook はアドレスを書かない。日時は「送信日時:」)
    internal = "差出人: 黒須 太郎\n送信日時: 2026年10月20日 9:00\n宛先: 山田 花子\n件名: RE: 清掃\n\n前の本文"
    # 宛先が2行に折り返したもの・CC
    wrapped = ("差出人: 山田 <yamada@example.com>\n送信日時: 2026年10月20日 9:00\n"
               "宛先: 鈴木 <suzuki@example.com>; 田中 <tanaka@example.com>;\n 佐藤 <sato@example.com>\n"
               "CC: info@example.com\n件名: RE: 清掃\n\n前の本文")
    # 宛先の続きの行の後に日時 (Outlook for Mac など「Date:」の書き方)
    mac = ("From: Taro <t@example.com>\nTo: a@example.com; b@example.com;\n c@example.com\n"
           "Date: Monday, October 27, 2026 at 10:00\nSubject: x\n\nold")
    out = _split(client, {
        "orig": ["了解です\n\n-----Original Message-----\nFrom: a@example.com\nSent: x\n\n前の本文", {}],
        "orig_jp": ["了解です\n\n----- 元のメッセージ -----\n前の本文", {}],
        # Outlook: 見出しの上の線から下を全部
        "outlook": ["鈴木様\n\nありがとうございます。\n\n________________________________\n" + outlook, {}],
        "outlook_en": ["Thanks\n\nFrom: Taro <t@example.com>\nSent: Monday, October 27, 2026 10:00 AM\n"
                       "To: info@example.com\nSubject: RE: x\n\nold", {}],
        "internal": ["了解です。\n\n" + internal, {}],
        "wrapped": ["了解です。\n\n" + wrapped, {}],
        "mac": ["Thanks\n\n" + mac, {}],
        # 見出しの前の「>」の引用も畳む (別々に。誰が書いたかの行があるもの)
        "both": ["はい\n\n山田さんは書きました:\n" + QUOTE + "\n\nいいえ\n\n-----Original Message-----\n前", {}],
    })
    assert out["orig"] == [(False, "了解です"),
                           (True, "-----Original Message-----\nFrom: a@example.com\nSent: x\n\n前の本文")]
    assert out["orig_jp"] == [(False, "了解です"), (True, "----- 元のメッセージ -----\n前の本文")]
    assert out["outlook"] == [(False, "鈴木様\n\nありがとうございます。"),
                              (True, "________________________________\n" + outlook)]
    assert out["outlook_en"] == [
        (False, "Thanks"),
        (True, "From: Taro <t@example.com>\nSent: Monday, October 27, 2026 10:00 AM\n"
               "To: info@example.com\nSubject: RE: x\n\nold")]
    assert out["internal"] == [(False, "了解です。"), (True, internal)]
    assert out["wrapped"] == [(False, "了解です。"), (True, wrapped)]
    assert out["mac"] == [(False, "Thanks"), (True, mac)]
    assert out["both"] == [(False, "はい"), (True, "山田さんは書きました:\n" + QUOTE), (False, "いいえ"),
                           (True, "-----Original Message-----\n前")]


def test_new_text_is_never_folded(client):
    """相手が新しく書いた所 (依頼・予約の知らせの中身・答え) は、引用に見えても畳まない."""
    cases = {
        # HPのお問い合わせの知らせ (フォーム経由と分からないもの): 送信者・メールアドレス・送信日時・件名の項目
        "form_lead": "ホームページからお問い合わせがありました。\n\n送信者: 山田\nメールアドレス: yamada@example.com\n"
                     "送信日時: 2026/11/03 10:00\n件名: 清掃のご依頼\n\nお問い合わせ内容:\n来週の清掃をお願いしたいです。",
        # 同じ形で、送信者の行にアドレスがあるもの (宛先の行が無いので見出しではない)
        "form_addr": "ホームページからお問い合わせがありました。\n\n送信者: 山田 <yamada@example.com>\n"
                     "送信日時: 2026/11/03 10:00\n件名: 清掃のご依頼\n\nお問い合わせ内容:\n来週の清掃をお願いしたいです。",
        # 予約の知らせ: 「差出人:」「日時:」「件名:」の項目 (「日時:」は送った日時ではない)
        "booking": "新しいご予約が入りました。\n━━━━━━━━\n差出人: Airbnb\n日時: 2026年11月3日 15:00\n"
                   "件名: 予約確定\n━━━━━━━━\nゲスト: 山田様\n清掃を手配してください。",
        "notice": "予約の知らせです。\n\n差出人: 予約システム <noreply@example.com>\n日時: 2026年11月3日 15:00\n"
                  "宛先: 清掃チーム\n件名: 予約確定\n\nゲスト: 山田様\n清掃を手配してください。",
        # 文の中の From / To / Date (アドレスも「Sent:」も無い)
        "prose": "移動の件です。\nFrom: 渋谷区の物件\nTo: 新宿区の物件\nDate: 11月3日\nよろしくお願いします。",
        # 見出しの行の間に空行がある (続けて並んでいない)
        "gap": "了解\n\nFrom: Taro <t@example.com>\n\nSent: Monday\nTo: a@example.com\nSubject: x\n\nold",
        # 「書きました」で終わる文 (「:」が無い)・日付で始まり「：」で終わる文 (時刻が無い)
        "wrote_text": "了解です。\n報告書を書きました\n> a\n> b\n> c\n> d",
        "wrote_text_mid": "報告書を書きました\n" + QUOTE + "\nご確認ください。",
        "date_text": "了解です。\n2026年11月3日の作業予定は以下のとおりです：\n> 鍵の受け取り\n> 清掃\n> 点検\n> 完了の報告",
        # 「＞」の箇条書き (本文の途中・一番下)
        "bullets": "お世話になります。\n以下の内容でお願いします。\n＞日程：11月3日\n＞場所：渋谷\n＞人数：3名\n"
                   "＞備考：鍵あり\nよろしくお願いします。",
        "bullets_end": "お世話になります。\n以下の内容でお願いします。\n\n＞日程：11月3日\n＞場所：渋谷\n"
                       "＞人数：3名\n＞備考：鍵あり",
        # 引用と答えを交互に書いたもの (質問の引用だけ隠れて、答えだけが残らないように)
        "inline": "山田様\n\n> 1. 鍵の受け渡しはどうしますか？\n> 2. 清掃は何時からですか？\n> 3. ゴミはどうしますか？\n"
                  "> 4. 駐車場はありますか？\n1は大丈夫です。2もOK。\n> 5. 備品は？\n> 6. 支払いは？\n3と4は確認します。",
        "inline_attr": "山田さんは書きました:\n> 1. 鍵\n> 2. 時間\n> 3. ゴミ\n> 4. 駐車場\n1は大丈夫です。2もOK。\n"
                       "> 5. 備品\n3と4は確認します。",
        # 誰が書いたかの行が無い「>」の所が一番下にあっても、上の文とのあいだに空行が無ければ畳まない
        "no_gap": "了解です。\n" + QUOTE,
    }
    out = _split(client, {k: [t, {}] for k, t in cases.items()})
    for name, text in cases.items():
        assert out[name] == [(False, text)], name


def test_attribution_does_not_take_new_text(client):
    """「〜 wrote:」の行のすぐ上の文 (新しく書いた所) を、2行に折り返した続きとして畳まない."""
    q4 = "> a\n> b\n> c\n> d"
    jp_attr = "2026年10月27日(月) 10:00 山田太郎 <taro@example.com>:"
    cases = {
        "jp": ["承知しました。\n2026年11月3日の10時に伺います。", "山田さんは書きました:"],
        "en": ["OK.\nOn Monday we can come at 10:30.", "Taro wrote:"],
        "en_nodot": ["OK\nOn Monday at 10:30 we can come", "Taro wrote:"],
        "en_year": ["OK\nOn Nov 3, 2026 we can come at 10:30", "Taro wrote:"],
        "gmail_jp": ["承知しました。\n2026/11/03 は10時に伺います", jp_attr],
        "gmail_jp_time": ["承知しました。\n2026/11/03 10:00 に伺います", jp_attr],
        "addr": ["承知しました。\n明日の10:00に伺います。", "<taro@example.com> wrote:"],
    }
    out = _split(client, {k: [new + "\n" + attr + "\n" + q4, {}] for k, (new, attr) in cases.items()})
    for name, (new, attr) in cases.items():
        assert out[name] == [(False, new), (True, attr + "\n" + q4)], name


def test_long_quotes_and_inline_replies(client):
    long_q = "\n".join(f"> 前のメールの{i}行目" for i in range(1, 13))
    sig = "--\n黒須"
    cases = {
        # とても長い引用は、誰が書いたかの行が無くても・本文の途中でも畳む
        "long_mid": "はい\n\n" + long_q + "\n\nよろしくお願いします。",
        # 交互に書いたメールでも、とても長い所は畳む (短い質問の引用は出したまま)
        "inline_long": "> 質問1\n答え1\n" + long_q + "\n答え2",
        # 交互に書いた後の、一番下の前のやり取りは畳む
        "inline_tail": "> 10時でいいですか？\nはい、大丈夫です。\n\n"
                       "On Mon, Oct 27, 2026 at 10:00 AM Taro <taro@example.com> wrote:\n" + QUOTE,
        # 誰が書いたかの行がある引用は、下に署名があっても畳む
        "attr_sig": "承知しました。\n\n山田さんは書きました:\n" + QUOTE + "\n\n" + sig,
    }
    out = _split(client, {k: [t, {}] for k, t in cases.items()})
    assert out["long_mid"] == [(False, "はい"), (True, long_q), (False, "よろしくお願いします。")]
    assert out["inline_long"] == [(False, "> 質問1\n答え1"), (True, long_q), (False, "答え2")]
    assert out["inline_tail"] == [
        (False, "> 10時でいいですか？\nはい、大丈夫です。"),
        (True, "On Mon, Oct 27, 2026 at 10:00 AM Taro <taro@example.com> wrote:\n" + QUOTE)]
    assert out["attr_sig"] == [(False, "承知しました。"), (True, "山田さんは書きました:\n" + QUOTE),
                               (False, sig)]


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


def test_split_quotes_is_fast_on_huge_input(client):
    """とても長い本文 (1行2万文字・2万行など) でも、すぐに分けられる (調べ方が行数の2乗にならない)."""
    out = _run(client.get("/mail").text, r"""
const rep = (s, k) => Array(k).fill(s).join("\n");
const C = {
  at: "@".repeat(20000), spaces: " ".repeat(20000), dashes: "-".repeat(20000),
  marks: rep(">", 20000), from: rep("差出人: a@b", 5000), hdr: rep("From: a@b\nTo: c@d", 3000),
  runs: rep("> a\nb", 5000), attr: rep("2026/10/27 " + "1:11 ".repeat(55) + ":\n> a\n> b\n> c\n> d", 60),
  wrote: "a".repeat(20000) + " wrote:\n" + rep("> a", 5),
};
const o = {};
for (const [k, t] of Object.entries(C)) {
  const t0 = process.hrtime.bigint();
  splitQuotes(t);
  o[k] = Number(process.hrtime.bigint() - t0) / 1e6;
}
console.log(JSON.stringify(o));""")
    slow = {k: ms for k, ms in out.items() if ms > 500}
    assert not slow, slow


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
