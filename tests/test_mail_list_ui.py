"""受信箱の行の ☆ (★フラグ)・状態の印・受信日時のテスト (画面の「一覧の行:」の部分を node で動かす).

- fmtListDate: 会社のタイムゾーンで、今日「20:15」・6日以内「木 20:15」・今年「10/23」・前の年「2025/10/23」。
  タイムゾーンの境目 (UTC の 15:30 は日本の次の日) も、日本の日付で数える
- statusMark: 「↩ 返信済み」と、いつ・誰が (Webメール・Gmail から返信したものは「Webメールから」)、
  「✓ 対応不要」と、いつ・誰が (前の版が対応不要にしたものは日付だけ・時刻も無ければ印だけ)、
  確認待ちは頼んだ人、差し戻したメールは「差し戻し」
- 外から来た文字 (件名・名前) は esc() を通す (★のボタンの読み上げの名前・状態の欄)
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


@pytest.fixture()
def page(client):
    return client.get("/mail").text


def _cut(page: str, start: str, end: str, include_end: bool = True) -> str:
    i = page.index(start)
    j = page.index(end, i)
    return page[i:j + len(end) if include_end else j]


def _run(page: str, scenario: str, timezone: str = "Asia/Tokyo"):
    """画面の esc・PR・ST・snip・fmtTz・fmtDate と「一覧の行:」の部分を node で動かし、
    scenario が出した JSON を返す (会社のタイムゾーンは appSettings.timezone)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    i = page.index("const esc = s =>")
    parts = [
        f"const appSettings = {{ timezone: {json.dumps(timezone)} }};\n",
        page[i:page.index("\n", page.index("c =>", i)) + 1],
        _cut(page, "const PR = ", "};", include_end=True) + "\n",
        _cut(page, "const ST = ", "};", include_end=True) + "\n",
        _cut(page, "const snip = ", "\n};", include_end=True) + "\n",
        _cut(page, "function fmtTz(", "\n// 未対応の数", include_end=False),
        _cut(page, "// 一覧の行: ここから", "// 一覧の行: ここまで", include_end=False),
    ]
    r = subprocess.run([node, "-e", "".join(parts) + "\n" + scenario], capture_output=True,
                       text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# 今 = 2026/10/29(木) 20:00 (日本時間)
NOW = "2026-10-29T11:00:00Z"


def _dates(page, cases: dict, now: str = NOW, timezone: str = "Asia/Tokyo") -> dict:
    return _run(page, f"const C = {json.dumps(cases)};\n"
                      f"const now = new Date({json.dumps(now)});\n"
                      "const o = {};\n"
                      "for (const [k, v] of Object.entries(C)) o[k] = fmtListDate(v, now);\n"
                      "console.log(JSON.stringify(o));", timezone)


def test_list_date_today_week_year_and_older(page):
    out = _dates(page, {
        "today": "2026-10-29T01:15:00Z",          # 日本の 10/29 10:15
        "today_midnight": "2026-10-28T15:05:00Z",  # 日本の 10/29 00:05 (UTC ではまだ前の日)
        "yesterday": "2026-10-28T12:30:00Z",      # 日本の 10/28(水) 21:30
        "six_days": "2026-10-23T03:00:00Z",       # 日本の 10/23(金) 12:00
        "seven_days": "2026-10-22T03:00:00Z",     # 1週間より前は日付だけ
        "this_year": "2026-03-05T00:00:00Z",
        "new_year_utc": "2025-12-31T15:30:00Z",   # 日本では 2026/1/1
        "last_year": "2025-10-23T00:00:00Z",
        "future": "2026-11-02T00:00:00Z",          # 先の日付 (時計の進んだ差出人) は日付
        "empty": "",
        "broken": "not a date",
    })
    assert out == {
        "today": "10:15", "today_midnight": "00:05", "yesterday": "水 21:30",
        "six_days": "金 12:00", "seven_days": "10/22", "this_year": "3/5",
        "new_year_utc": "1/1", "last_year": "2025/10/23", "future": "11/2",
        "empty": "", "broken": "",
    }


def test_list_date_counts_days_in_company_timezone(page):
    """UTC の 15:30 は日本の次の日。ブラウザの時計ではなく、会社のタイムゾーンの日付で数える."""
    # 今 = 日本の 10/30(金) 00:30 (UTC ではまだ 10/29)
    now = "2026-10-29T15:30:00Z"
    out = _dates(page, {"late": "2026-10-29T14:00:00Z",      # 日本の 10/29(木) 23:00 = 昨日
                        "same": "2026-10-29T15:10:00Z"}, now)  # 日本の 10/30 00:10 = 今日
    assert out == {"late": "木 23:00", "same": "00:10"}
    # 会社のタイムゾーンを変えると、その日付で数える (ロサンゼルスでは同じ 10/29)
    out = _dates(page, {"late": "2026-10-29T14:00:00Z", "same": "2026-10-29T15:10:00Z"}, now,
                 timezone="America/Los_Angeles")
    assert out == {"late": "07:00", "same": "08:10"}


def _marks(page, cases: dict) -> dict:
    return _run(page, f"const C = {json.dumps(cases, ensure_ascii=False)};\n"
                      "const o = {};\n"
                      "for (const [k, m] of Object.entries(C)) o[k] = statusMark(m);\n"
                      "console.log(JSON.stringify(o));")


# 前の年の日時 (「2025/10/29」と出る。テストを走らせる日によらない)
AT = "2025-10-29T05:03:00Z"   # 日本の 2025/10/29 14:03


def test_status_mark_replied_from_here_webmail_and_gmail(page):
    out = _marks(page, {
        "app": {"status": "replied", "replied_at": AT, "replied_by": "黒須"},
        "webmail": {"status": "replied", "replied_at": AT, "replied_by": "Webメール",
                    "reply_source": "webmail"},
        "gmail": {"status": "replied", "replied_at": AT, "replied_by": "Gmail",
                  "reply_source": "gmail"},
        # 前の版のサーバ (誰が返信したかを一覧に返さない) なら日付だけ
        "old_server": {"status": "replied", "replied_at": AT},
    })
    assert out["app"] == ('<span class="st-chip st-replied" title="2025/10/29 14:03 に 黒須 が返信しました">'
                          '↩ 返信済み</span><small class="st-sub">2025/10/29・黒須</small>')
    assert out["webmail"] == (
        '<span class="st-chip st-replied" title="Webメールから返信してあります'
        '（送信済みフォルダから取り込んで、自動で返信済みにしました）">↩ 返信済み</span>'
        '<small class="st-sub">2025/10/29・Webメールから</small>')
    assert out["gmail"].endswith('<small class="st-sub">2025/10/29・Gmailから</small>')
    assert 'title="Gmailから返信してあります' in out["gmail"]
    assert out["old_server"] == ('<span class="st-chip st-replied" title="2025/10/29 14:03 に 返信しました">'
                                 '↩ 返信済み</span><small class="st-sub">2025/10/29</small>')


def test_status_mark_archived_pending_and_returned(page):
    out = _marks(page, {
        "archived": {"status": "archived", "archived_at": AT, "archived_by": "黒須"},
        # 前の版が対応不要にしたメール: 人が空なら日付だけ、時刻も無ければ印だけ
        "archived_old": {"status": "archived", "archived_at": AT, "archived_by": ""},
        "archived_no_time": {"status": "archived", "archived_at": None, "archived_by": ""},
        "pending": {"status": "pending_approval", "requested_by": "鈴木"},
        "pending_nobody": {"status": "pending_approval", "requested_by": ""},
        "returned": {"status": "unhandled", "returned": True},
        "unhandled": {"status": "unhandled", "returned": False},
    })
    assert out["archived"] == ('<span class="st-chip st-archived" title="返信しなくてよい印です'
                               '（2025/10/29 14:03 黒須）">✓ 対応不要</span>'
                               '<small class="st-sub">2025/10/29・黒須</small>')
    assert out["archived_old"] == ('<span class="st-chip st-archived" title="返信しなくてよい印です'
                                   '（2025/10/29 14:03）">✓ 対応不要</span>'
                                   '<small class="st-sub">2025/10/29</small>')
    assert out["archived_no_time"] == ('<span class="st-chip st-archived" title="返信しなくてよい印です">'
                                       '✓ 対応不要</span>')
    assert out["pending"] == ('<span class="st-chip st-pending_approval">確認待ち</span>'
                              '<small class="st-sub">（鈴木）</small>')
    assert out["pending_nobody"] == '<span class="st-chip st-pending_approval">確認待ち</span>'
    assert out["returned"] == ('<span class="st-chip st-unhandled">未対応</span><span class="ret-badge" '
                               'title="管理者が差し戻しました（返信画面にコメントがあります）">差し戻し</span>')
    assert out["unhandled"] == '<span class="st-chip st-unhandled">未対応</span>'


def test_names_and_subjects_are_escaped(page):
    bad = '<img src=x onerror=alert(1)>"\''
    out = _run(page, f"const bad = {json.dumps(bad)};\n"
                     "console.log(JSON.stringify({"
                     " star: starButton({ id: 7, subject: bad, flagged: true, flagged_by: bad,"
                     "                    flagged_at: '2025-10-29T05:03:00Z' }),"
                     " off: starButton({ id: '8\"x', subject: bad, flagged: false, flagged_at: null }),"
                     " old: starButton({ id: 9, subject: 'x' }),"
                     " replied: statusMark({ status: 'replied', replied_at: '2025-10-29T05:03:00Z',"
                     "                       replied_by: bad }),"
                     " archived: statusMark({ status: 'archived', archived_at: '2025-10-29T05:03:00Z',"
                     "                        archived_by: bad }),"
                     " pending: statusMark({ status: 'pending_approval', requested_by: bad }),"
                     " odd: statusMark({ status: bad }),"
                     " icon: repliedIcon({ status: 'replied', replied_at: '2025-10-29T05:03:00Z',"
                     "                     replied_by: bad }),"
                     " time: listTime('2025-10-29T05:03:00Z\"><b>'),"
                     "}));")
    for k, html in out.items():
        assert "<img" not in html and "<b>" not in html, (k, html)
    assert "&lt;img src=x onerror=alert(1)&gt;&quot;&#39;" in out["star"]
    # ★が付いているか・付けた人と時刻 (指したときの説明)
    assert 'class="row-flag" data-flag="7" aria-pressed="true"' in out["star"]
    assert 'aria-label="★フラグを外す（&lt;img src=x onerror=alert(1)&gt;&quot;&#39;）"' in out["star"]
    assert ('title="★フラグ：&lt;img src=x onerror=alert(1)&gt;&quot;&#39;（2025/10/29 14:03）。押すと外します"'
            in out["star"])
    # 付いていないとき (番号は数字にする)
    assert 'data-flag="NaN" aria-pressed="false"' in out["off"]
    assert 'aria-label="★フラグを付ける（' in out["off"]
    assert 'title="★フラグを付けると、みんなの画面で目立ちます"' in out["off"]
    # 前の版のサーバ (★を返さない) なら ☆ を出さない
    assert out["old"] == ""
    assert out["icon"].startswith('<span class="rep-ic" aria-hidden="true" title="返信済み（2025/10/29・')
    # 外から来た文字は、どこにも生のまま入らない
    assert "onerror=alert(1)&gt;" in out["replied"] and "onerror=alert(1)&gt;" in out["odd"]


def test_star_label_uses_short_subject(page):
    long_subject = "清掃のご依頼について" * 10
    out = _run(page, f"const s = {json.dumps(long_subject, ensure_ascii=False)};\n"
                     "console.log(JSON.stringify({"
                     " a: starText({ subject: s, flagged: false }),"
                     " b: starText({ subject: '', flagged: true, flagged_by: '', flagged_at: null }),"
                     "}));")
    assert out["a"]["label"] == "★フラグを付ける（" + long_subject[:30] + "…）"
    assert out["a"]["on"] is False
    assert out["b"] == {"on": True, "label": "★フラグを外す（件名なし）", "title": "★フラグ。押すと外します"}


def test_list_time_shows_company_time_with_weekday(page):
    out = _run(page, "console.log(JSON.stringify({ t: listTime('2026-10-29T11:15:00Z'),"
                     " none: listTime(null) }));")
    assert out["t"] == ('<time datetime="2026-10-29T11:15:00Z" title="2026/10/29(木) 20:15">'
                        '<span class="d-long">2026/10/29 20:15</span><span class="d-short">'
                        + out["t"].split('<span class="d-short">')[1])
    assert out["none"] == ""
