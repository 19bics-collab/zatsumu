"""受信箱のスレッドの画面のテスト (画面の「スレッド:」「一覧の行の形:」の部分を node で動かす・画面の中身).

- groupThreads: 先頭の行は一覧の最初のメール (新しい順でも重要度順でも)。閉じたスレッドは、未対応・確認待ち・★・
  この画面で触った・右に出しているメールだけを古い順に出す。開いたスレッドは読んだスレッドの木の順で、
  段はパソコン3段・スマホ2段まで、一覧に無いものは「条件外」。「▶ 5通」の畳んだ数・出ていない未対応と★・
  畳んだ中の新着・添付。1通だけなら「▶」は無い。1通ずつのときはそのまま。数が合わないときは畳まない
- 外から来た文字 (件名・名前) は、続きの行でも「このスレッド」でも esc() を通す
- 画面: 名前がぶつからない・切り替え・この画面で触ったメールは閉じても出す・使い方・設定
"""
import json
import re
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


@pytest.fixture()
def page(client):
    return client.get("/mail").text


def _cut(page: str, start: str, end: str, include_end: bool = True) -> str:
    i = page.index(start)
    j = page.index(end, i)
    return page[i:j + len(end) if include_end else j]


def _block(page: str, start: str, end: str = "\n}\n") -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def _run(page: str, scenario: str):
    """画面の esc・hl・日時と「受信箱」「一覧の行:」「スレッド:」「一覧の行の形:」の部分を node で動かす."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    i = page.index("const esc = s =>")
    hl_end = page.index("\n}\n", page.index("function hl(")) + 3
    stubs = """
const appSettings = { timezone: "Asia/Tokyo" };
let mailStatus = null;
const peekOpen = new Set();
const peekButton = () => "";
const archiveButton = () => "";
const peekRowHtml = () => "";
const hitSnip = () => "";
let seenId = 100;
const openedIds = new Set();
"""
    parts = [
        stubs,
        page[i:page.index("\n", page.index("c =>", i)) + 1],
        page[page.index("const qTerms = "):hl_end],
        _cut(page, "const PR = ", "};") + "\n",
        _cut(page, "const ST = ", "};") + "\n",
        _cut(page, "const snip = ", "\n};") + "\n",
        _cut(page, "function fmtTz(", "\n// 未対応の数", include_end=False),
        _cut(page, "/* ---------- 受信箱 ---------- */", "// 一覧の行: ここから", include_end=False),
        _cut(page, "// 一覧の行: ここから", "// 一覧の行: ここまで", include_end=False),
        _cut(page, "// スレッド: ここから", "// スレッド: ここまで", include_end=False),
        _cut(page, "// 一覧の行の形: ここから", "// 一覧の行の形: ここまで", include_end=False),
    ]
    helpers = """
const M = (id, more = {}) => ({ id, subject: "s" + id, status: "replied", priority: 2,
  received_at: new Date(Date.UTC(2026, 9, 1, 0, id)).toISOString(), th_key: 1, th_size: 5, th_open: 0,
  th_flagged: 0, th_newest_id: 5, th_first_at: "2026-10-01T00:01:00Z", attachment_count: 0, ...more });
const view = rows => rows.map(r => r.note ? `note:${r.note}` :
  `${r.m.id}${r.head ? "H" : ""}${r.off ? "o" : ""}${r.ind ? ":" + r.ind : ""}`);
"""
    r = subprocess.run([node, "-e", "".join(parts) + helpers + "\n" + scenario], capture_output=True,
                       text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_head_is_the_first_mail_in_list_order(page):
    """新しい順なら一番新しい、重要度順なら一番重要なメールが先頭の行。スレッドはその場所に置く."""
    out = _run(page, """
const date = [M(5), M(9, { th_key: 9, th_size: 1 }), M(4), M(3)];        // 新しい順
const prio = [M(3, { priority: 1 }), M(9, { th_key: 9, th_size: 1 }), M(5), M(4)];   // 重要度順
console.log(JSON.stringify({ date: view(groupThreads(date, { on: true })),
                             prio: view(groupThreads(prio, { on: true })) }));""")
    assert out["date"] == ["5H", "9H"] and out["prio"] == ["3H", "9H"]


def test_collapsed_thread_shows_only_pinned_mails_oldest_first(page):
    out = _run(page, """
const list = [M(9), M(8), M(7, { status: "unhandled" }), M(6, { flagged: true }), M(5), M(4),
              M(3, { status: "pending_approval" }), M(2), M(1)].map(m => ({ ...m, th_size: 9 }));
const o = { on: true, keep: new Set([5]), sel: 2 };
const rows = groupThreads(list, o);
console.log(JSON.stringify({ rows: view(rows), pill: rows[0].pill,
                             none: view(groupThreads(list, { on: true })) }));""")
    # 未対応 (7)・確認待ち (3)・★ (6)・この画面で触った (5)・右に出している (2) だけ、古い順に
    assert out["rows"] == ["9H", "2:1", "3:1", "5:1", "6:1", "7:1"]
    assert out["none"] == ["9H", "3:1", "6:1", "7:1"]
    p = out["pill"]
    assert p["size"] == 9 and p["rest"] == 3 and p["button"] is True and p["open"] is False


def test_expanded_thread_uses_tree_order_depth_caps_and_off_rows(page):
    out = _run(page, """
const list = [M(5), M(3)];               // 一覧に出ているのは 5 と 3 だけ (ほかは絞り込みの外)
const items = [M(1, { depth: 0 }), M(2, { depth: 1 }), M(3, { depth: 2 }), M(4, { depth: 5 }),
               M(5, { depth: 6 })];
const cache = new Map([[1, { items, sig: "x" }]]);
const o = { on: true, expanded: new Set([1]), cache };
const desk = groupThreads(list, o), card = groupThreads(list, { ...o, cards: true });
const loading = groupThreads(list, { on: true, expanded: new Set([1]) });
const failed = groupThreads(list, { on: true, cache: new Map([[1, { error: "load", opened: true }]]) });
console.log(JSON.stringify({ desk: view(desk), card: view(card), loading: view(loading),
                             failed: view(failed), pill: desk[0].pill,
                             same: desk[3].m === list[1] }));""")
    # 先頭の行 (5) の下に、木の順で 5 以外。一覧に無いもの (1・2・4) は条件外。段は 1 + 深さ (3段・2段まで)
    assert out["desk"] == ["5H", "1o:1", "2o:2", "3:3", "4o:4"]
    assert out["card"] == ["5H", "1o:1", "2o:2", "3:3", "4o:3"]
    assert out["same"] is True            # 一覧にあるメールは一覧の項目 (★・対応不要がそろう)
    assert out["pill"]["open"] is True and out["pill"]["button"] is True and out["pill"]["rest"] == 0
    # 読み込み中は、一覧にある分を古い順と「読み込み中…」。読めなかったら閉じて知らせる
    assert out["loading"] == ["5H", "3:1", "note:loading"]
    assert out["failed"] == ["5H", "note:error"]


def test_pill_counts_hidden_open_flagged_new_and_attachments(page):
    out = _run(page, """
const list = [M(5, { th_open: 3, th_flagged: 2, status: "unhandled" }), M(4, { flagged: true }),
              M(2, { attachment_count: 1 }), M(101), M(1)];
const isNew = m => m.id > 100;
const rows = groupThreads(list, { on: true, isNew });
const replied = groupThreads(list, { on: true, isNew, status: "replied" });
const html = thrPillHtml(rows[0].pill, 1);
const one = groupThreads([M(7, { th_key: 7, th_size: 1 })], { on: true });
const all = groupThreads([M(5, { th_size: 2, status: "unhandled" }), M(4, { th_size: 2, status: "unhandled" })], { on: true });
console.log(JSON.stringify({ pill: rows[0].pill, rows: view(rows), replied: replied[0].pill.openHidden,
                             html, one: [view(one), one[0].pill], all: [view(all), all[0].pill],
                             allHtml: thrPillHtml(all[0].pill, 1) }));""")
    p = out["pill"]
    # 出ているのは 5 (未対応) と 4 (★)。畳んだのは 3 通。一覧に出ていない未対応が2通・★が1通
    assert out["rows"] == ["5H", "4:1"]
    assert (p["rest"], p["openHidden"], p["flagHidden"], p["newFolded"], p["attFolded"]) == (3, 2, 1, True, True)
    assert out["replied"] == 0            # 「返信済み」だけを出しているときは、未対応の数は出さない
    h = out["html"]
    assert 'class="th-tg" data-thr-tg="1" aria-expanded="false"' in h and "▶ 5通" in h
    assert '<span class="th-more">・未対応2・★1</span>' in h and 'class="th-new"' in h and "📎" in h
    assert "このスレッドのメール5通を開く（ほかに未対応・確認待ちが2通）（ほかに★が1通）" in h
    assert "最初のメール: 2026/10/1(木) 09:01" in h
    # 1通だけのスレッドは「▶」なし。全部出ているときはボタンではなく「2通」だけ
    assert out["one"] == [["7H"], None]
    assert out["all"][0] == ["5H", "4:1"] and out["all"][1]["button"] is False
    assert out["allHtml"].startswith('<span class="th-n"') and "2通" in out["allHtml"]


def test_flat_mode_and_inconsistent_counts_never_hide_mails(page):
    out = _run(page, """
const list = [M(5), M(4), M(3)];
const flat = groupThreads(list, { on: false, expanded: new Set([1]) });
// 通数が一覧に出ている数より少ない (数えている間に消えた など): 畳まずに全部出す
const odd = groupThreads(list.map(m => ({ ...m, th_size: 2 })), { on: true });
const old = groupThreads([{ id: 8, subject: "前の版のサーバ", status: "replied" }, { id: 7, status: "replied" }], { on: true });
console.log(JSON.stringify({ flat: view(flat), pills: flat.map(r => r.pill), odd: view(odd),
                             old: view(old) }));""")
    assert out["flat"] == ["5H", "4H", "3H"] and out["pills"] == [None, None, None]
    assert out["odd"] == ["5H", "3:1", "4:1"]
    assert out["old"] == ["8H", "7H"]     # th_* の無い (前の版の) サーバは1通ずつ


def test_outside_text_is_escaped_in_child_rows_and_thread_strip(page):
    out = _run(page, """
const evil = '<img src=x onerror=alert(1)>"\\'';
const list = [M(5, { status: "unhandled" }), M(3, { status: "unhandled", subject: evil, contact_name: evil })];
const rows = groupThreads(list, { on: true, expanded: new Set([1]),
  cache: new Map([[1, { items: [M(1, { subject: evil, from_name: evil, depth: 0 }), M(3, { depth: 1 })] }]]) });
const html = rows.map(r => r.note ? thrNoteRow(r) : mailRowHtml(r.m, r, { admin: true, T: null })).join("");
const strip = pvConvHtml([M(1, { subject: evil, contact_name: evil, depth: 0 }),
                          M(3, { subject: evil, depth: 9, flagged: true, attachment_count: 2 })], 3, 7);
console.log(JSON.stringify({ html, strip }));""")
    for h in (out["html"], out["strip"]):
        assert "<img" not in h and "onerror=alert(1)>" not in h
        assert "&lt;img src=x onerror=alert(1)&gt;" in h
    h = out["html"]
    assert h.count('class="mail-row') == 3 and 'data-th="1" style="--ind:1" data-id="1"' in h
    assert 'class="sr-only">スレッドの続き: </span><span class="off-tag"' in h and ">条件外</span>" in h
    assert " th-head" in h and " th-child off" in h
    s = out["strip"]
    assert "<h3>このスレッド（メール 7通・古い順）</h3>" in s
    assert 'data-pv-open="3" aria-current="true"' in s and "（いま見ているメール）" in s
    assert 'style="--depth:3"' in s and "古いメールが、ほかに 5 通あります" in s


# ---------- 画面 ----------

def test_page_has_thread_controls_and_markers(page):
    for marker in ("// スレッド: ここから", "// スレッド: ここまで", "// 一覧の行の形: ここから",
                   'data-thr-tg="${Number(key)}"', 'data-thr-mode="on"', 'data-thr-mode="off"',
                   'const THR_KEY = "zatsumu_mail_threads";', ">条件外</span>", 'id="pv-conv"',
                   'r.headers.get("X-Thread-Mode") === "1"', 'id="pv-back"', "このスレッド（メール"):
        assert marker in page, marker
    tg = page[page.index('<div class="sort-tg" id="thr-tg"'):]
    tg = tg[:tg.index("</div>")]
    assert "スレッド" in tg and "まとめる" in tg and "1通ずつ" in tg and " hidden>" in tg
    # 設定でスレッドを止めているときは切り替えを出さない。探している間は1通ずつ
    assert '$("thr-tg").hidden = !(mailStatus && mailStatus.thread_mode);' in page
    assert "const thrOn = () => thrServer && thrWanted && !inboxFound;" in page
    assert "探している間は、スレッドにまとめずに1通ずつ出します" in page


def test_names_do_not_collide(page):
    for name in ("threadOpen", "thrExpanded", "thrCache", "groupThreads", "statusMark", "starButton",
                 "selectRow", "pvShow", "keepShown", "thrToggle", "thrLoad", "mailRowHtml", "pvConvHtml"):
        assert len(re.findall(rf"(?:let|const|function)\s+{name}\b", page)) == 1, name
    assert page.count('data-thr="') == 1


def test_render_inbox_groups_and_keeps_its_guards(page):
    r = _block(page, "function renderInbox(", '$("filter").onchange')
    assert "const rows = groupThreads(list, {" in r
    assert "keep: keepShown, sel: paneOn() ? selectedId : null" in r
    # 押し間違いの対策は、出す行の並び (スレッドの続きも) で比べる。開け閉めでは止めない
    assert "if (keepPlace && !opts.userToggle) {" in r and "before !== drawn" in r
    assert r.index("rowsMovedAt = Date.now();") < r.index("tbody.innerHTML")
    # 「▶ 5通」は返信画面を開かず、行が動いた直後は押しても何もしない
    m = re.search(r'tr\.onclick = e => \{(.*?)\n    \}\);', r, re.S).group(1)
    assert 'const g = e.target.closest("button.th-tg");' in m and "if (g) e.stopPropagation();" in m
    assert m.index("if (g) e.stopPropagation();") < m.index("ROW_QUIET_MS") < m.index("thrToggle(")
    assert '"data-thr-tg"' in re.search(r"const ROW_BTN_KEYS = \[(.*?)\];", page).group(1)
    # 開いたスレッドの一覧に無いメールも、出しているメールに入れる (★・対応不要・本文 ▼ が使える)
    assert "if (r.off && !shownMails.has(Number(r.m.id))) shownMails.set(Number(r.m.id), r.m);" in r
    assert "thrAfterRender(list);" in r and "pvAfterRender();" in r
    assert "スレッド ${heads}件（メール ${list.length}通）を出しています" in r


def test_touched_mails_stay_visible_in_closed_threads(page):
    for start in ("async function archiveFromRow(b, tr) {", "async function archiveFromPane() {",
                  '$("m-archive").onclick = async () => {', "async function toggleFlag(id, on) {",
                  '$("m-send").onclick = async () => {', '$("m-request").onclick = async () => {',
                  '$("m-return").onclick = async () => {'):
        assert "keepShown.add(id);" in _block(page, start), start
    t = _block(page, "function thrToggle(key, open) {")
    assert "for (const m of thrMembers(key)) keepShown.delete(Number(m.id));" in t
    assert "renderInbox(lastInboxList, true, { userToggle: true, anchorId: head ? head.id : null });" in t


def test_pane_strip_and_keyboard(page):
    k = page[page.index('document.addEventListener("keydown", e => {\n  if ($("app").hidden || !paneOn()'):]
    k = k[:k.index("\n});")]
    assert '(k === "ArrowRight" || k === "ArrowLeft") && !inPane && selectedId' in k
    assert 'thrArrow(k === "ArrowRight")' in k
    r = _block(page, "function renderPreview(d, same) {")
    assert "renderPvConv(true);" in r
    c = _block(page, "function renderPvConv(retry = false) {")
    assert "pvConvHtml(c.items, pv.id, c.total)" in c and "if (!item || !thrOn() || size <= 1)" in c
    g = _block(page, "function pvGo(id, back = false) {")
    assert "pv.history.push(pv.id)" in g and "selectRow(id, { now: true, keepHistory: true });" in g
    s = _block(page, "function markSelected() {")
    assert 'head.classList.add("sel-in");' in s


def test_css_keeps_touch_targets_and_phone_width(page):
    assert "@media (pointer: coarse) { .th-tg { min-height: 40px; padding: .45rem .8rem; } }" in page
    mobile = page[page.index("/* スマホのカード: スレッドの続きのカードは少し右へ"):]
    mobile = mobile[:mobile.index("\n  }\n")]
    assert ".table-wrap tr.mail-row.th-child { margin-left: calc(min(var(--ind, 1), 3) * .6rem);" in mobile
    assert ".th-tg { min-height: 40px;" in mobile
    assert "#inbox.pane-mode tr.mail-row.th-child { background-image:" in page
    assert ".pv-conv li > button {" in page and "min-height: 44px;" in page


def test_help_and_settings_explain_threads(page):
    howto = page[page.index('data-howto="inbox"'):]
    howto = howto[:howto.index("</details>")]
    for text in ("1つの「スレッド」にまとめます", "「▶ 5通」を押すと", "未対応・確認待ち・★のメールは、閉じていても必ず出します",
                 "本文の上の「このスレッド」", "HPフォームから届いた別々のお客様のメールはまとめません",
                 "「スレッド」で「1通ずつ」", "タブの数・未対応の数は、スレッドの数ではなくメールの数です"):
        assert text in " ".join(howto.split()) or text in howto, text
    assert 'id="set-mail-threads"' in page
    assert "受信箱で、同じ話のメール（返信どうし）を1つにまとめる（スレッド）" in page
    assert '$("set-mail-threads").checked = !!s.mail_thread_mode;' in page
    assert 'mail_thread_mode: $("set-mail-threads").checked,' in page


def test_pane_does_not_reload_when_only_the_list_knows_thread_size(page):
    """一覧の項目にはスレッドの通数 (th_size) があり、1通の詳細には無い。描き直しただけで (スレッドを
    開け閉めした) 右の本文を読み直さない。通数が変わったら (返事が届いた) 読み直す."""
    from tests.test_mail_list_ui import _run_pane
    out = _run_pane(page, """
  shownMails = new Map([[5, mail(5, { th_key: 1, th_size: 3 })]]);
  selectRow(5, { now: true });
  await sleep(5);
  await answer(0, 200, mail(5, { th_key: 1 }));            // 1通の詳細 (th_size が無い)
  const o = { th: pv.mail.th_size };
  pvAfterRender();                                          // スレッドを開け閉めして描き直した
  await sleep(5);
  o.same = calls.length;
  shownMails = new Map([[5, mail(5, { th_key: 1, th_size: 4 })]]);   // 返事が届いた
  pvAfterRender();
  await sleep(5);
  o.grown = calls.length;
  console.log(JSON.stringify(o));""")
    assert out == {"th": 3, "same": 1, "grown": 2}
