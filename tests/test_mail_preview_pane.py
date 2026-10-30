"""受信箱の右の本文 (広い画面で、一覧の行を押すと右に本文を出す) の画面のテスト.

画面 (server/templates/mail.html) の中身を文字で確かめる。選ぶ・読む・読み直すの動きは
tests/test_mail_list_ui.py (node で動かす)、ブラウザでの見た目は Playwright で確かめる。
- 横幅 1024px 以上で、「表示」が「右に本文」(既定。このブラウザに覚えておく) のときだけ出す。
  狭い画面 (スマホ) は今までどおり、行を押すと返信画面
- 本文は1通の詳細 (?thread=false) を読む。返信は今までの返信画面 (右の本文の上に重ねる)
- ↑↓ (j k)・Enter・Esc。文字を打つ所・日本語の変換中・返信画面を開いている間は使わない
- 1分ごとの取り直し・画面の下の知らせは、右の本文を出している間も止めない
- 返信画面: 続けて開いたときに前の答えで上書きしない・下書きを保存できなかったら知らせる・
  タブを隠したとき・ページを閉じるときも下書きを残す
"""
import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def page(tmp_path, monkeypatch):
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZATSUMU_LOGIN_VERIFY_EMAIL", raising=False)
    import importlib
    from server import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app).get("/mail").text


def _block(page: str, start: str, end: str = "\n}\n") -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def test_pane_only_on_wide_screens_and_remembers_choice(page):
    assert 'const PANE_MQ = matchMedia("(min-width: 1024px)");' in page
    assert 'const PANE_KEY = "zatsumu_mail_pane";' in page
    assert 'let paneWanted = loadPref(PANE_KEY) !== "off";' in page
    assert 'const paneOn = () => PANE_MQ.matches && paneWanted && viewName === "inbox";' in page
    # 「表示 [右に本文][一覧だけ]」(広い画面だけ出す)
    tg = page[page.index('id="pane-tg"'):]
    tg = tg[:tg.index("</div>")]
    assert 'data-pane-mode="on"' in tg and "右に本文" in tg and 'data-pane-mode="off"' in tg and "一覧だけ" in tg
    a = _block(page, "function applyLayout() {")
    assert '$("pane-tg").hidden = !PANE_MQ.matches;' in a
    assert 'document.documentElement.classList.toggle("pane-on", on);' in a
    assert '$("inbox").classList.toggle("pane-mode", on);' in a and '$("pv").hidden = !on;' in a
    assert 'savePref(PANE_KEY, on ? "on" : "off");' in page
    # 受信箱だけ (取引先・送信箱などでは出さない)。幅が変わったら (古い Safari も) 合わせる
    assert "applyLayout();   // 右の本文は受信箱だけ" in _block(page, "function switchView(name) {")
    assert 'PANE_MQ.addEventListener("change", layoutChanged)' in page
    assert "PANE_MQ.addListener(layoutChanged)" in page


def test_pane_markup(page):
    pv = page[page.index('<aside id="pv"'):page.index("</aside>")]
    for marker in ('id="pv-empty"', 'id="pv-reply"', 'id="pv-reply2"', 'id="pv-flag"', 'id="pv-archive"',
                   'id="pv-subject"', 'id="pv-meta"', 'id="pv-badges"', 'id="pv-body" class="mail-body"',
                   'id="pv-atts"', 'id="pv-reply-sent"', 'id="pv-stale"', 'id="pv-live"', 'id="pv-conv"'):
        assert marker in pv, marker
    assert 'tabindex="-1" hidden>' in pv[:pv.index(">") + 1]
    assert "左の一覧でメールを押すと、ここに本文が出ます" in pv
    assert 'aria-live="polite"' in pv
    # 一覧と右の本文は横に並べる (一覧は今までどおり画面ごと縦に進み、右の本文は貼り付く)
    assert '<div class="panes">' in page and '<div class="list-col">' in page
    assert "#inbox.pane-mode .panes { display: grid; grid-template-columns: var(--list-w) minmax(0, 1fr);" in page
    assert ":root { --list-w: clamp(340px, 34vw, 560px); }" in page
    css = page[page.index("  .pv { position: sticky;"):page.index("  .pv[hidden]")]
    assert "max-height: calc(100vh - 1rem); max-height: calc(100dvh - 1rem);" in css
    # 右の本文の中を終わりまで進めたら、続けて画面も進める (contain にすると、ページの一番上では
    # 画面の外にある右の本文の下の方 (添付・下の「↩ 返信する」) が見えない)
    assert "overflow-y: auto; overscroll-behavior: auto;" in css
    assert "overscroll-behavior: contain" not in css


def test_compact_rows_reuse_the_same_rows(page):
    """右に本文を出しているときの一覧は、同じ行を CSS だけで2行にする (表・スマホのカードと同じ行)."""
    r = _block(page, "function renderInbox(", '$("filter").onchange')
    for cls in ("c-pr", "c-from", "c-subj", "c-date", "c-st", "c-asg", "c-draft"):
        assert f'<td class="{cls}" data-label=' in r, cls
    assert 'data-id="${id}" tabindex="-1">' in r
    assert ('<span class="prb prb-in pr-${Number(m.priority)}">${esc(PR[m.priority] || m.priority)}</span>'
            '${repliedIcon(m)}') in r
    assert ".prb-in { display: none; }" in page
    assert ('grid-template-areas: "from date act" "subj st act";' in page and
            "#inbox.pane-mode #mail-table thead { display: none; }" in page)
    # 選んでいる行 (高の赤・新着の黄より強い青)
    assert "#mail-table .mail-row.sel, #mail-table .mail-row.sel td.act { background: #dbeafe !important; }" in page
    # スマホのカードは今までどおり (2行にするのは pane-mode のときだけ)
    mobile = page[page.index("@media (max-width: 640px) {"):]
    mobile = mobile[:mobile.index("\n  }\n")]
    assert "pane-mode" not in mobile


def test_row_click_selects_on_wide_screens_and_opens_reply_on_narrow(page):
    m = re.search(r'tr\.onclick = e => \{(.*?)\n    \}\);', page, re.S).group(1)
    assert ("else if (paneOn()) selectRow(+tr.dataset.id, { now: true });\n"
            "      else openMail(+tr.dataset.id);") in m
    # ☆・「本文 ▼」・「対応不要」と、行が動いた直後の押し間違いの対策は、右に出すときも同じ
    assert m.index("ROW_QUIET_MS") < m.index("selectRow(")


def test_pane_reads_detail_without_thread_and_guards_late_answers(page):
    s = _block(page, "async function pvShow(id, opts = {}) {")
    assert "await api(`/api/mail/${id}?thread=false`)" in s
    assert "const seq = ++pv.seq" in s and "if (seq !== pv.seq) return;" in s
    assert "if (selectedId !== id) return;" in s
    # 「新着」は、本文を出したときだけ外す (↑↓ で通り過ぎた行は外さない)
    shown = _block(page, "function pvShown(id, d, opts = {}) {")
    assert "markOpened(id);" in shown
    assert "markOpened(" not in _block(page, "function selectRow(")
    assert "const PV_WAIT_MS = 150;" in page
    assert "setTimeout(() => pvShow(id, { announce }), now ? 0 : PV_WAIT_MS);" in page


def test_pane_renders_outside_text_safely(page):
    r = _block(page, "function renderPreview(d, same) {")
    assert '$("pv-subject").textContent = d.subject || "（件名なし）";' in r
    assert 'bodyHtml(d.body, { subject: d.subject, form: d.via_form, key: "pv:" + id })' in r
    assert "${bodyHtml(d.reply_body)}" in r and "body.innerHTML = bodyHtml(d.draft_reply);" in r
    assert "el.textContent = text;" in r and "row.append(lbl, text);" in r
    assert '$("pv-client-name").textContent = ' in r and '$("pv-misdir").textContent = ' in r
    # innerHTML に入れる所は、外から来た文字を esc()・bodyHtml を通す
    for line in r.splitlines():
        if "innerHTML" in line:
            assert "bodyHtml(" in line or "${esc(" in line or line.rstrip().endswith("="), line
    a = _block(page, "function renderPvAttachments(d) {")
    assert "${esc(a.filename)}" in a and "${esc(a.reason)}" in a
    assert "${a.filename}" not in a and "${a.reason}" not in a
    f = _block(page, "function renderPvFlag(d) {")
    assert "${esc(d.assignee_name)}" in f and "${esc(who || \"フラグ\")}" in f
    assert re.findall(r"\$\{d\.", f) == []
    # 確認待ち・差し戻しの言葉は返信画面と同じ (approvalText)
    assert "const a = approvalText(d, admin), appr = $(\"pv-appr\");" in r
    assert "const { kind, head, note, help } = approvalText(m, admin);" in _block(page, "function renderApprovalBox(m) {")


def test_reply_opens_unchanged_modal_over_pane(page):
    assert '$("pv-reply").onclick = $("pv-reply2").onclick = () => { if (pv.id) openMail(pv.id).catch(() => {}); };' in page
    assert "html.pane-on #mail-modal { justify-content: flex-end;" in page
    assert "html.pane-on #mail-modal .box { box-sizing: border-box; width: max(560px, calc(100% - 2.5rem - var(--list-w)));" in page
    # 返信画面を閉じたら右の本文を読み直し、選んでいた行に戻る
    c = _block(page, "async function closeModal(opts = {}) {")
    assert "pvShow(pv.id, { keepScroll: true, quiet: true });" in c
    assert "mailRowOf(selectedId)?.focus({ preventScroll: true });" in c
    # 一覧の行 (暗い所) を押すと、閉じてそのメールを右に出す (1回で)
    b = _block(page, '$("mail-modal").onclick = async e => {', "\n};\n")
    assert "await closeModal();" in b and 'document.elementFromPoint(x, y)?.closest("#mail-table .mail-row")' in b
    assert "selectRow(Number(tr.dataset.id), { now: true });" in b
    # 返信画面の中の動き (下書きの保存・確認のお願い・知らせ) は今までどおり
    assert 'const anyModalOpen = () => !!document.querySelector(".modal:not([hidden])");' in page
    assert 'if (viewName === "inbox" && $("mail-modal").hidden) showInbox({ auto: true });' in page


def test_keyboard_is_gated(page):
    k = page[page.index('document.addEventListener("keydown", e => {\n  if ($("app").hidden || !paneOn()'):]
    k = k[:k.index("\n});")]
    assert "anyModalOpen()" in k and "e.isComposing || e.keyCode === 229" in k
    assert "e.ctrlKey || e.metaKey || e.altKey || e.shiftKey" in k
    assert 't.closest("input, textarea, select")' in k and "t.isContentEditable" in k
    assert '!t.closest("#mail-table")' in k
    for key in ('"ArrowDown"', '"ArrowUp"', '"j"', '"k"', '"Enter"', '"Escape"'):
        assert key in k, key
    # 右の本文の中の ↑↓ は本文を動かす。ボタンにいるときの Enter はそのボタン
    assert 'if (inPane && k.startsWith("Arrow")) return;' in k
    assert 't === document.body || t === $("pv") || t.matches(".mail-row")' in k
    assert "moveSelection(step);" in k and "openMail(selectedId)" in k and "clearSelection();" in k


def test_selection_survives_refresh_flag_and_archive(page):
    r = _block(page, "function renderInbox(", '$("filter").onchange')
    assert "pvAfterRender();" in r
    a = _block(page, "function pvAfterRender() {")
    assert "markSelected();" in a and "pvSig(m) !== pv.sig" in a
    assert "いまの絞り込みの一覧には出ていません" in a
    # ★・対応不要は、右の本文にもそろえる (読み直さない)
    assert "pvMailChanged(id, f);" in _block(page, "function setFlagUi(id, at, by) {")
    assert "pvMailChanged(id, done);" in _block(page, "function showRowArchived(id, r = {}) {")
    f = _block(page, '$("pv-flag").onclick = () => {', "\n};\n")
    assert "toggleFlag(pv.id, !pv.mail.flagged);" in f
    # 描き直してもキーボードの位置 (選んだ行) を戻す
    f = _block(page, "function focusedRowButton(box) {")
    assert 'b.matches(".mail-row")' in f
    # 右の対応不要: 行は残して灰色に。「未対応」だけを出しているときは次のメールを右に出す
    p = _block(page, "async function archiveFromPane() {")
    assert 'archiveMail(id, "unhandled")' in p and "showRowArchived(id, r);" in p
    assert "showArchivedToast(id, d.subject, r);" in p and "refreshInbox();" in p
    assert 'if (selectedId === id && next && paneOn()) selectRow(next, { now: true });' in p
    # 戻るで来たときに、右に出していたメールをまた出す
    assert "if (restore && st.sel && paneOn()) selectRow(st.sel, { now: true, focus: false });" in page


def test_toasts_and_refresh_keep_working_while_pane_is_shown(page):
    """右の本文は返信画面 (.modal) ではないので、知らせも1分ごとの取り直しも止めない."""
    assert 'class="pv"' in page and '<aside id="pv" class="pv modal' not in page
    assert "html.pane-on #toasts { left: 1.5rem; transform: none;" in page
    r = _block(page, "function refreshInbox() {")
    assert 'if (viewName === "inbox" && $("mail-modal").hidden) showInbox({ auto: true });' in r
    assert "pv" not in r


def test_open_mail_guard_and_draft_hardening(page):
    o = _block(page, "async function openMail(id) {")
    assert "const seq = ++openSeq;" in o and "if (seq !== openSeq) return;" in o
    assert o.index("if (seq !== openSeq) return;") < o.index("markOpened(id);")
    assert "openSeq++;" in _block(page, "async function closeModal(opts = {}) {")
    s = _block(page, "async function saveDraft(opts = {}) {")
    assert 'if (!r.ok) throw new Error("draft " + r.status);' in s
    # keepalive の上限 (64KB) はバイト数 (日本語は1文字3バイト。文字数で比べると、長い下書きを保存できない)
    assert "bytes = new TextEncoder().encode(json).length" in s
    assert "keepalive: !!opts.keepalive && bytes < 60000" in s
    assert "json.length < 60000" not in s
    assert "書きかけの返信文を保存できませんでした（そのメールを開き直して確かめてください）" in s
    assert 'window.addEventListener("pagehide", saveDraftNow);' in page
    assert 'document.addEventListener("visibilitychange", () => { if (document.hidden) saveDraftNow(); });' in page
    assert "saveDraft({ keepalive: true })" in page
    # 「元に戻す」の無い知らせ (閉じるボタン付き。文字は textContent)
    n = _block(page, "function noticeToast(msg) {")
    assert "text.textContent = msg;" in n and "makeRoomForToast(box);" in n


def test_help_explains_the_pane(page):
    howto = page[page.index('data-howto="inbox"'):]
    howto = howto[:howto.index("</details>")]
    for text in ("行を押すと、広い画面では右に本文が出ます（スマホでは返信画面が開きます）。",
                 "広い画面（パソコン・iPad の横向き）では、一覧のメールを押すと、右側に本文が出ます。",
                 "キーボードの ↑ ↓ で前後のメールに移り",
                 "Enter で返信画面を開きます",
                 "右上の「↩ 返信する」を押してください",
                 "返信画面が右側に重なって開きます。閉じると、元の一覧と本文に戻ります",
                 "「表示」で「一覧だけ」を押してください（このブラウザに覚えておきます）",
                 "スマホでは今までどおり、押すと返信画面が開きます。"):
        assert text in howto, text
    assert "行をクリックすると本文の確認・返信文の生成/編集・送信ができます。" not in page
    # 管理者だけの説明の外 (メール担当者にも出す)
    assert howto.index("右側に本文が出ます") < howto.index('<span id="admin-help">')


def test_names_are_declared_once(page):
    """右の本文の名前が、今の「この相手とのやり取り」(threadOpen・data-thr) とぶつからない."""
    for name in ("threadOpen", "selectedId", "selectRow", "pvShow", "pvSig", "nextRowId", "applyLayout",
                 "statusMark", "starButton", "setFlagButton", "approvalText", "saveBlob"):
        assert len(re.findall(rf"(?:let|const|function)\s+{name}\b", page)) == 1, name
    assert page.count('data-thr="') == 1


def test_draft_failure_notice_goes_away_after_a_later_save(page):
    """書きかけの返信文を保存できなかった知らせは、後で同じメールを保存できたら消す
    (保存できているのに「保存できませんでした」と残さない)。同じメールの知らせは1つだけ."""
    n = _block(page, "function noticeToast(msg) {")
    assert "return close;" in n
    s = _block(page, "async function saveDraft(opts = {}) {")
    ok = s[s.index('if (!r.ok) throw new Error("draft " + r.status);'):s.index("} catch (e) {")]
    assert "draftFailNotice.get(id)?.();" in ok and "draftFailNotice.delete(id);" in ok
    fail = s[s.index("} catch (e) {"):]
    assert fail.index("draftFailNotice.get(id)?.();") < fail.index("draftFailNotice.set(id,")
    assert "noticeToast(\"書きかけの返信文を保存できませんでした" in fail
    assert "const draftFailNotice = new Map();" in page


def test_pane_flag_ignores_clicks_right_after_archive_button_moves(page):
    """右の「対応不要にする」を2回押したつもりで、ずれてきた「☆ フラグ」や、次に出したメールの
    「対応不要にする」まで押さない (同じメールで「対応不要にする」が消えた・出た直後の ROW_QUIET_MS は、
    「☆ フラグ」「対応不要にする」を押しても何もしない)."""
    r = _block(page, "function renderPreview(d, same) {")
    assert 'const arch = $("pv-archive"), canArchive = admin && d.status === "unhandled";' in r
    assert "if (same && arch.hidden === canArchive) pv.actMovedAt = Date.now();" in r
    assert r.index("pv.actMovedAt = Date.now();") < r.index("arch.hidden = !canArchive;")
    f = _block(page, '$("pv-flag").onclick = () => {', "\n};\n")
    assert "Date.now() - pv.actMovedAt < ROW_QUIET_MS) return;" in f
    assert f.index("ROW_QUIET_MS") < f.index("toggleFlag(")
    # 「未対応」だけを出しているときは、次のメールの「対応不要にする」が同じ所に出る (それも押さない)
    a = _block(page, '$("pv-archive").onclick = () => {', "\n};\n")
    assert "if (Date.now() - pv.actMovedAt >= ROW_QUIET_MS) archiveFromPane();" in a
    # 右の本文の「対応不要にする」は、行の「対応不要」と同じく showRowArchived → pvMailChanged で消える
    assert "renderPreview(pv.mail, true);" in _block(page, "function pvMailChanged(id, fields) {")


def test_double_click_opens_reply_only_on_the_selected_row(page):
    """行をダブルクリックで返信画面を開くのは、1回目で選んだ行だけ。ボタン・右端の「対応不要」の所と、
    行が動いた・「対応不要」が消えた直後 (ROW_QUIET_MS) は開かない (「対応不要」を2回押したつもりで、
    対応不要にしたメールの返信画面を開かないように)."""
    d = _block(page, 'document.querySelector("#mail-table tbody").addEventListener("dblclick", e => {',
               "\n});\n")
    assert 'e.target.closest("button, td.act")' in d
    assert "Number(tr.dataset.id) !== selectedId" in d
    assert "Date.now() - rowsMovedAt < ROW_QUIET_MS) return;" in d
    assert d.index("ROW_QUIET_MS") < d.index("openMail(")


def test_keyboard_and_layout_do_nothing_on_the_login_screen(page):
    """ログアウトした後 (ログイン画面・確認コードの画面) は、↑ ↓ などで前の人の一覧を選ばない・
    幅を変えても受信箱を取り直さない (トークン無しで頼んで、ログイン画面に「正しくない」と出さない)."""
    k = page[page.index('document.addEventListener("keydown", e => {\n  if ($("app").hidden || !paneOn()'):]
    assert k.index('$("app").hidden') < k.index("moveSelection(step);")
    lc = _block(page, "function layoutChanged() {")
    assert 'peekOpen.size && !$("app").hidden) refreshInbox();' in lc


def test_back_after_logout_does_not_restore_previous_users_mail(page):
    """右に出していた・開いていたメールは、今ログインしている回に残したものだけ「戻る」で出し直す
    (ログアウトして別の人がログインした後に「戻る」を押しても、前の人が見ていたメールを出さない)."""
    assert 'const LOGIN_MARK_KEY = "zatsumu_mail_login_mark";' in page
    assert "newLoginMark();" in _block(page, "function showLogin(withError) {")
    n = _block(page, "function newLoginMark() {")
    assert "sessionStorage.setItem(LOGIN_MARK_KEY, loginMark)" in n and "try {" in n
    assert "history.replaceState({ ...st, sel: id, lg: loginMark }" in _block(page, "function pvRemember(id) {")
    assert "history.replaceState({ ...cur, mail: id, lg: loginMark }" in page
    p = _block(page, 'window.addEventListener("popstate", async e => {', "\n});\n")
    assert "if ((st.sel || st.mail) && st.lg !== loginMark) {" in p
    assert "const { sel: _s, mail: _m, lg: _l, ...rest } = st;" in p
    assert p.index("st.lg !== loginMark") < p.index("renderState(st, true);")
