import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_tz():
    # timezone はプロセス全体の状態なので、設定変更テストの影響を残さない
    from server import tz
    tz.set_tz("Asia/Tokyo")
    yield
    tz.set_tz("Asia/Tokyo")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(tmp_path))
    # app.py reads the env var at import time, so re-import per test
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


def auth(user):
    return {"Authorization": f"Bearer {user['token']}"}


def test_clock_in_out_flow(client, users):
    worker, _ = users
    assert client.post("/api/clock-in", headers=auth(worker)).status_code == 200
    # double clock-in rejected
    assert client.post("/api/clock-in", headers=auth(worker)).status_code == 409
    assert client.post("/api/clock-out", headers=auth(worker)).status_code == 200
    assert client.post("/api/clock-out", headers=auth(worker)).status_code == 409


def test_journal_member_and_admin(client, users):
    worker, admin = users
    # 初期は空
    assert client.get("/api/me/journal", headers=auth(worker)).json()["body"] == ""

    # 保存 (日付指定)
    r = client.put("/api/me/journal", headers=auth(worker),
                   json={"date": "2026-05-10", "body": "  現場対応をしました  "})
    assert r.status_code == 200
    assert client.get("/api/me/journal?date=2026-05-10",
                      headers=auth(worker)).json()["body"] == "現場対応をしました"

    # 管理者は日付ごとに全員分を取得 (未提出も含む)
    j = client.get("/api/journals?date=2026-05-10", headers=auth(admin)).json()
    assert j["date"] == "2026-05-10"
    by = {e["name"]: e for e in j["entries"]}
    assert by["tanaka"]["body"] == "現場対応をしました"
    assert by["boss"]["body"] == ""  # 未提出

    # 一般ユーザーは一覧・他人の日報を見られない
    assert client.get("/api/journals?date=2026-05-10",
                      headers=auth(worker)).status_code == 403
    assert client.get(f"/api/users/{worker['id']}/journal?date=2026-05-10",
                      headers=auth(worker)).status_code == 403
    assert client.get(f"/api/users/{worker['id']}/journal?date=2026-05-10",
                      headers=auth(admin)).json()["body"] == "現場対応をしました"

    # 本文を空にすると削除される
    client.put("/api/me/journal", headers=auth(worker),
               json={"date": "2026-05-10", "body": "  "})
    assert client.get("/api/me/journal?date=2026-05-10",
                      headers=auth(worker)).json()["body"] == ""

    # 不正な日付は400
    assert client.get("/api/me/journal?date=bad",
                      headers=auth(worker)).status_code == 400


def test_journal_flag_in_monthly(client, users):
    worker, admin = users
    client.put("/api/me/journal", headers=auth(worker),
               json={"date": "2026-05-03", "body": "日報テスト"})
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                        headers=auth(admin)).json()
    days = {d["date"]: d for d in detail["days"]}
    # 在席が無くても日報がある日は含まれ、has_journal が立つ
    assert days["2026-05-03"]["has_journal"] is True


def test_journal_email_notify(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    sent = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: sent.append(text))
    # 既定で notify_journal は ON。初回保存で本人名+本文を通知先へ送る
    r = client.put("/api/me/journal", headers=auth(worker),
                   json={"date": "2026-05-01", "body": "清掃3件完了"})
    assert r.status_code == 200
    assert len(sent) == 1
    assert "tanaka" in sent[0] and "清掃3件完了" in sent[0]
    # 同じ日の編集では連投しない (1人1日1回)
    client.put("/api/me/journal", headers=auth(worker),
               json={"date": "2026-05-01", "body": "追記：明日は9時"})
    assert len(sent) == 1
    # OFF にすると通知しない
    client.patch("/api/settings", headers=auth(admin), json={"notify_journal": False})
    client.put("/api/me/journal", headers=auth(worker),
               json={"date": "2026-05-02", "body": "別日の報告"})
    assert len(sent) == 1
    # notify_enabled OFF のメンバーは (ON に戻しても) 通知しない
    client.patch("/api/settings", headers=auth(admin), json={"notify_journal": True})
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"notify_enabled": False})
    client.put("/api/me/journal", headers=auth(worker),
               json={"date": "2026-05-03", "body": "通知OFFメンバーの日報"})
    assert len(sent) == 1


def test_journal_clear_resave_no_double_notify(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    sent = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: sent.append(text))
    d = {"date": "2026-05-05"}
    client.put("/api/me/journal", headers=auth(worker), json={**d, "body": "初回の日報"})
    assert len(sent) == 1
    # 空にして保存: 行は残り notified_at を保つ / has_journal からは外れる
    client.put("/api/me/journal", headers=auth(worker), json={**d, "body": "   "})
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                        headers=auth(admin)).json()
    days = {x["date"]: x for x in detail["days"]}
    assert not (days.get("2026-05-05") or {}).get("has_journal")
    # 書き直して保存しても同じ日は再通知しない
    client.put("/api/me/journal", headers=auth(worker), json={**d, "body": "書き直した日報"})
    assert len(sent) == 1


def test_auth_required(client, users):
    assert client.post("/api/clock-in").status_code == 401
    assert client.post(
        "/api/clock-in", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_screenshot_requires_clocked_in(client, users):
    worker, _ = users
    files = {"image": ("s.jpg", b"\xff\xd8fake", "image/jpeg")}
    assert client.post(
        "/api/screenshots", headers=auth(worker), files=files
    ).status_code == 409
    client.post("/api/clock-in", headers=auth(worker))
    r = client.post("/api/screenshots", headers=auth(worker), files=files)
    assert r.status_code == 200
    sid = r.json()["screenshot_id"]

    _, admin = users
    img = client.get(f"/api/screenshots/{sid}/image", headers=auth(admin))
    assert img.status_code == 200
    assert img.content == b"\xff\xd8fake"


def test_status_admin_only(client, users):
    worker, admin = users
    assert client.get("/api/status", headers=auth(worker)).status_code == 403

    client.post("/api/clock-in", headers=auth(worker))
    r = client.get("/api/status", headers=auth(admin))
    assert r.status_code == 200
    by_name = {u["name"]: u for u in r.json()}
    assert by_name["tanaka"]["seated"] is True
    assert by_name["boss"]["seated"] is False
    assert by_name["tanaka"]["hours_today"] >= 0


def test_admin_page(client, users):
    # 静的ページなので誰でも取得可能。データは API 側の Bearer 認証で守る
    r = client.get("/admin")
    assert r.status_code == 200
    assert "稼働状況" in r.text
    assert "ログイン" in r.text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_redirects_to_admin(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/admin"


def test_me_and_member_page(client, users):
    worker, _ = users
    assert client.get("/api/me").status_code == 401

    r = client.get("/api/me", headers=auth(worker))
    assert r.status_code == 200
    assert r.json()["name"] == "tanaka"
    assert r.json()["seated"] is False

    client.post("/api/clock-in", headers=auth(worker))
    me = client.get("/api/me", headers=auth(worker)).json()
    assert me["seated"] is True
    assert me["open_since"] is not None
    assert me["hours_today"] >= 0

    client.post("/api/clock-out", headers=auth(worker))
    assert client.get("/api/me", headers=auth(worker)).json()["seated"] is False

    page = client.get("/me")
    assert page.status_code == 200
    assert "打刻" in page.text
    assert "勤務実績" in page.text


def test_me_monthly(client, users, tmp_path):
    worker, _ = users
    _record_session(tmp_path, worker["id"], "2026-05-01T00:00:00+00:00",
                    "2026-05-01T03:00:00+00:00")  # JST 5/1 09:00-12:00
    r = client.get("/api/me/monthly?month=2026-05", headers=auth(worker))
    assert r.status_code == 200
    data = r.json()
    assert data["user"]["name"] == "tanaka"
    days = {d["date"]: d for d in data["days"]}
    assert days["2026-05-01"]["hours"] == 3.0
    # 認証なしは不可
    assert client.get("/api/me/monthly?month=2026-05").status_code == 401


def test_teams(client, users):
    worker, admin = users
    # 作成は管理者のみ
    assert client.post("/api/teams", headers=auth(worker),
                       json={"name": "営業部"}).status_code == 403
    t = client.post("/api/teams", headers=auth(admin), json={"name": "営業部"}).json()
    assert client.post("/api/teams", headers=auth(admin),
                       json={"name": "営業部"}).status_code == 409  # 重複

    # メンバーをチームに割り当て
    r = client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                     json={"team_id": t["id"]})
    assert r.json()["team_id"] == t["id"]
    # 不明なチームは拒否
    assert client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                        json={"team_id": 9999}).status_code == 400

    # 稼働状況にチーム名が出る & チームで絞り込める
    st = client.get("/api/status", headers=auth(admin)).json()
    assert {u["name"]: u["team_name"] for u in st}["tanaka"] == "営業部"
    filtered = client.get(f"/api/status?team_id={t['id']}",
                          headers=auth(admin)).json()
    assert [u["name"] for u in filtered] == ["tanaka"]
    teams = client.get("/api/teams", headers=auth(admin)).json()
    assert {x["name"]: x["members"] for x in teams}["営業部"] == 1

    # チーム削除でメンバーは未所属に戻る
    client.delete(f"/api/teams/{t['id']}", headers=auth(admin))
    assert client.get("/api/users", headers=auth(admin)).json()
    by = {u["name"]: u for u in client.get("/api/users", headers=auth(admin)).json()}
    assert by["tanaka"]["team_id"] is None

    # clear_team で未所属に戻せる
    t2 = client.post("/api/teams", headers=auth(admin), json={"name": "開発"}).json()
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"team_id": t2["id"]})
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"clear_team": True})
    by = {u["name"]: u for u in client.get("/api/users", headers=auth(admin)).json()}
    assert by["tanaka"]["team_id"] is None


def test_leave_request_and_approval(client, users):
    worker, admin = users
    assert "有給休暇" in client.get("/api/leave/types",
                                 headers=auth(worker)).json()["types"]

    # 申請
    r = client.post("/api/me/leave", headers=auth(worker),
                    json={"date": "2026-07-01", "leave_type": "有給休暇",
                          "reason": "私用"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    # 不明な種別は拒否
    assert client.post("/api/me/leave", headers=auth(worker),
                       json={"date": "2026-07-02", "leave_type": "X"}).status_code == 400

    # 本人は自分の申請を見られる
    mine = client.get("/api/me/leave", headers=auth(worker)).json()
    assert mine[0]["date"] == "2026-07-01" and mine[0]["status"] == "pending"

    # 管理者は pending 一覧を取得し承認
    pend = client.get("/api/leave?status=pending", headers=auth(admin)).json()
    assert len(pend) == 1 and pend[0]["name"] == "tanaka"
    lid = pend[0]["id"]
    # 一般ユーザーは承認不可
    assert client.post(f"/api/leave/{lid}/decision", headers=auth(worker),
                       json={"approve": True}).status_code == 403
    assert client.post(f"/api/leave/{lid}/decision", headers=auth(admin),
                       json={"approve": True}).json()["status"] == "approved"

    # 承認済みは取り消せない & 上書き申請もできない
    assert client.delete(f"/api/me/leave/{lid}",
                         headers=auth(worker)).status_code == 409
    assert client.post("/api/me/leave", headers=auth(worker),
                       json={"date": "2026-07-01",
                             "leave_type": "欠勤"}).status_code == 409

    # 個人月次に休暇が反映される
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-07",
                        headers=auth(admin)).json()
    days = {d["date"]: d for d in detail["days"]}
    assert days["2026-07-01"]["leave"] == {"type": "有給休暇", "status": "approved"}

    # 未承認の申請は本人が取り消せる
    client.post("/api/me/leave", headers=auth(worker),
                json={"date": "2026-07-05", "leave_type": "半休"})
    lid2 = [x for x in client.get("/api/me/leave", headers=auth(worker)).json()
            if x["date"] == "2026-07-05"][0]["id"]
    assert client.delete(f"/api/me/leave/{lid2}",
                         headers=auth(worker)).status_code == 200


def test_user_management(client, users):
    worker, admin = users
    # 一覧は管理者のみ。トークンは含まれない
    assert client.get("/api/users", headers=auth(worker)).status_code == 403
    lst = client.get("/api/users", headers=auth(admin)).json()
    # 管理画面でトークンを常時表示するため、一覧にもトークンを含める
    assert all("token" in u for u in lst)

    # 追加 (トークンは作成時のみ返る)。重複名は409
    r = client.post("/api/users", headers=auth(admin),
                    json={"name": "yamada", "is_admin": False})
    assert r.status_code == 200
    created = r.json()
    assert created["token"]
    assert client.post("/api/users", headers=auth(admin),
                       json={"name": "yamada"}).status_code == 409

    # 新ユーザーのトークンで打刻できる
    h = {"Authorization": f"Bearer {created['token']}"}
    assert client.post("/api/clock-in", headers=h).status_code == 200
    client.post("/api/clock-out", headers=h)

    # トークン再発行 → 旧トークンは無効に
    r2 = client.post(f"/api/users/{created['id']}/token", headers=auth(admin))
    assert r2.status_code == 200
    assert client.post("/api/clock-in", headers=h).status_code == 401
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    assert client.get("/api/me", headers=h2).status_code == 200

    # 無効化 → 認証不可。有効化で復帰
    client.patch(f"/api/users/{created['id']}", headers=auth(admin),
                 json={"active": False})
    assert client.get("/api/me", headers=h2).status_code == 401
    client.patch(f"/api/users/{created['id']}", headers=auth(admin),
                 json={"active": True})
    assert client.get("/api/me", headers=h2).status_code == 200

    # 管理者付与/解除
    r3 = client.patch(f"/api/users/{created['id']}", headers=auth(admin),
                      json={"is_admin": True})
    assert r3.json()["is_admin"] == 1

    # 自分自身の無効化・降格は拒否
    me = [u for u in client.get("/api/users", headers=auth(admin)).json()
          if u["name"] == "boss"][0]
    assert client.patch(f"/api/users/{me['id']}", headers=auth(admin),
                        json={"active": False}).status_code == 400
    assert client.patch(f"/api/users/{me['id']}", headers=auth(admin),
                        json={"is_admin": False}).status_code == 400


def test_force_clock_out(client, users):
    worker, admin = users
    # 着席していないと409
    assert client.post(f"/api/users/{worker['id']}/clock-out",
                       headers=auth(admin)).status_code == 409
    client.post("/api/clock-in", headers=auth(worker))
    assert client.post(f"/api/users/{worker['id']}/clock-out",
                       headers=auth(worker)).status_code == 403  # 管理者のみ
    assert client.post(f"/api/users/{worker['id']}/clock-out",
                       headers=auth(admin)).status_code == 200
    assert client.get("/api/me", headers=auth(worker)).json()["seated"] is False


def test_session_edit_add_delete_and_audit(client, users):
    worker, admin = users
    # 手動追加
    r = client.post(f"/api/users/{worker['id']}/sessions", headers=auth(admin),
                    json={"clock_in": "2026-05-01T09:00", "clock_out": "2026-05-01T18:00"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    # 退席 <= 着席 は拒否
    assert client.post(f"/api/users/{worker['id']}/sessions", headers=auth(admin),
                       json={"clock_in": "2026-05-01T18:00",
                             "clock_out": "2026-05-01T09:00"}).status_code == 400

    # JST解釈の確認: 月次に9時間が乗る
    rows = {x["name"]: x for x in client.get(
        "/api/reports/monthly?month=2026-05", headers=auth(admin)).json()["rows"]}
    assert rows["tanaka"]["total_hours"] == 9.0

    # 修正
    assert client.patch(f"/api/sessions/{sid}", headers=auth(admin),
                        json={"clock_in": "2026-05-01T09:00",
                              "clock_out": "2026-05-01T12:00"}).status_code == 200
    rows = {x["name"]: x for x in client.get(
        "/api/reports/monthly?month=2026-05", headers=auth(admin)).json()["rows"]}
    assert rows["tanaka"]["total_hours"] == 3.0

    # 個人ページのセグメントに編集用の情報が乗る
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                        headers=auth(admin)).json()
    seg = detail["days"][0]["sessions"][0]
    assert seg["id"] == sid
    assert seg["clock_in"].startswith("2026-05-01T09:00")

    # 削除
    assert client.delete(f"/api/sessions/{sid}",
                         headers=auth(admin)).status_code == 200
    assert client.delete(f"/api/sessions/{sid}",
                         headers=auth(admin)).status_code == 404

    # 一般ユーザーは打刻修正不可
    assert client.patch(f"/api/sessions/{sid}", headers=auth(worker),
                        json={"clock_in": "x", "clock_out": "y"}).status_code == 403

    # 修正履歴CSVに操作が記録されている
    month = __import__("datetime").datetime.now().strftime("%Y-%m")
    csv = client.get(f"/api/reports/audit.csv?month={month}", headers=auth(admin))
    assert csv.status_code == 200
    assert "打刻追加" in csv.text
    assert "打刻修正" in csv.text
    assert "打刻削除" in csv.text
    assert "boss" in csv.text


def test_status_includes_today_sessions(client, users):
    worker, admin = users
    client.post("/api/clock-in", headers=auth(worker))
    by_name = {u["name"]: u for u in client.get(
        "/api/status", headers=auth(admin)).json()}
    assert len(by_name["tanaka"]["today_sessions"]) == 1
    assert by_name["tanaka"]["today_sessions"][0]["open"] is True
    assert by_name["boss"]["today_sessions"] == []


def test_company_settings_and_public_config(client, users):
    worker, admin = users
    # 公開設定は認証なしで取得できる (ログイン画面の表示用)
    cfg = client.get("/api/config").json()
    assert cfg == {"company_name": "zatsumu", "work_start": "09:00",
                   "work_end": "18:00"}

    # 会社名・勤務時間帯の変更
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"company_name": "アクメ商事", "work_start": "10:00",
                           "work_end": "19:00"})
    assert r.status_code == 200
    assert r.json()["company_name"] == "アクメ商事"
    assert client.get("/api/config").json() == {
        "company_name": "アクメ商事", "work_start": "10:00", "work_end": "19:00"}

    # バリデーション
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"company_name": "   "}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"work_start": "25:00"}).status_code == 400


def test_timezone_setting_affects_aggregation(client, users, tmp_path):
    worker, admin = users
    # UTC 23:30-翌0:30 の打刻
    _record_session(tmp_path, worker["id"], "2026-05-01T23:30:00+00:00",
                    "2026-05-02T00:30:00+00:00")

    # 既定(Asia/Tokyo)では JST 5/2 08:30-09:30 = 5/2 に1時間
    rows = {r["name"]: r for r in client.get(
        "/api/reports/monthly?month=2026-05", headers=auth(admin)).json()["rows"]}
    assert rows["tanaka"]["total_hours"] == 1.0

    # UTC に切り替えると 5/1 に0.5h, 5/2 に0.5h
    client.patch("/api/settings", headers=auth(admin), json={"timezone": "UTC"})
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                        headers=auth(admin)).json()
    days = {d["date"]: d["hours"] for d in detail["days"]}
    assert days["2026-05-01"] == 0.5
    assert days["2026-05-02"] == 0.5

    # 不正なタイムゾーンは拒否
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"timezone": "Nowhere/Land"}).status_code == 400


def test_settings_get_patch_and_effective(client, users):
    worker, admin = users
    # 取得は管理者のみ
    assert client.get("/api/settings", headers=auth(worker)).status_code == 403
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["capture_min_interval"] == 300
    assert s["retention_days"] == 30

    # 変更 → 本人用の実効設定に反映される
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"capture_min_interval": 60, "capture_max_interval": 120,
                           "capture_quality": 80, "retention_days": 7})
    assert r.status_code == 200
    eff = client.get("/api/me/settings", headers=auth(worker)).json()
    assert eff == {"min_interval": 60, "max_interval": 120, "quality": 80,
                   "blur": 0, "capture_enabled": True}

    # バリデーション: 範囲外 / min > max
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"capture_quality": 5}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"capture_min_interval": 500,
                              "capture_max_interval": 100}).status_code == 400

    # メンバー個人の撮影停止 → 実効設定が false に
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"capture_enabled": False})
    assert client.get("/api/me/settings",
                      headers=auth(worker)).json()["capture_enabled"] is False
    # 全社停止でも false (個人ONに戻しても全社がOFFなら停止)
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"capture_enabled": True})
    client.patch("/api/settings", headers=auth(admin),
                 json={"capture_enabled": False})
    assert client.get("/api/me/settings",
                      headers=auth(worker)).json()["capture_enabled"] is False

    # 設定変更が修正履歴に残る
    month = __import__("datetime").datetime.now().strftime("%Y-%m")
    csv = client.get(f"/api/reports/audit.csv?month={month}", headers=auth(admin))
    assert "設定変更" in csv.text
    assert "撮影ON/OFF" in csv.text


def test_retention_uses_settings(client, users):
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"retention_days": 1})
    r = client.post("/api/admin/purge", headers=auth(admin))
    assert r.json()["retention_days"] == 1


def test_daily_csv(client, users, tmp_path):
    worker, admin = users
    # JST 5/1 09:00-12:00 (3h) と 5/2 09:00-10:30 (1.5h)
    _record_session(tmp_path, worker["id"], "2026-05-01T00:00:00+00:00",
                    "2026-05-01T03:00:00+00:00")
    _record_session(tmp_path, worker["id"], "2026-05-02T00:00:00+00:00",
                    "2026-05-02T01:30:00+00:00")
    r = client.get("/api/reports/daily.csv?month=2026-05", headers=auth(admin))
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0].lstrip("﻿") == "日付,tanaka,合計"
    assert "2026-05-01,3.0,3.0" in r.text   # 右端に各日の合計
    assert "2026-05-02,1.5,1.5" in r.text
    assert len([l for l in lines if l.startswith("2026-05")]) == 31  # 全日分
    # 末尾に各メンバーの月合計行 (tanaka=4.5, 総合計=4.5)
    assert lines[-1].startswith("合計,4.5") and lines[-1].endswith(",4.5")


def test_target_hours_overtime_shortfall(client, users, tmp_path):
    worker, admin = users
    # JST 5/1 09:00-19:00 = 10h (残業2h), 5/2 09:00-15:00 = 6h (不足2h)
    _record_session(tmp_path, worker["id"], "2026-05-01T00:00:00+00:00",
                    "2026-05-01T10:00:00+00:00")
    _record_session(tmp_path, worker["id"], "2026-05-02T00:00:00+00:00",
                    "2026-05-02T06:00:00+00:00")
    # 既定の予定時間=8h
    rep = client.get("/api/reports/monthly?month=2026-05", headers=auth(admin)).json()
    assert rep["target_hours"] == 8.0
    row = {r["name"]: r for r in rep["rows"]}["tanaka"]
    assert row["work_days"] == 2
    assert row["total_hours"] == 16.0
    assert row["target_hours"] == 16.0   # 2日 × 8h
    assert row["overtime"] == 2.0        # 5/1 の +2h
    assert row["shortfall"] == 2.0       # 5/2 の -2h

    # 予定時間を7時間に変更すると残業/不足が変わる
    client.patch("/api/settings", headers=auth(admin),
                 json={"daily_target_minutes": 420})
    row = {r["name"]: r for r in client.get(
        "/api/reports/monthly?month=2026-05", headers=auth(admin)).json()["rows"]}["tanaka"]
    assert row["overtime"] == 3.0   # 5/1:+3, 5/2:-1
    assert row["shortfall"] == 1.0

    # 個人月次の各日に over (過不足) が出る
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                        headers=auth(admin)).json()
    assert detail["target_hours"] == 7.0
    days = {d["date"]: d for d in detail["days"]}
    assert days["2026-05-01"]["over"] == 3.0
    assert days["2026-05-02"]["over"] == -1.0

    # CSV にも列が出る
    csv = client.get("/api/reports/monthly.csv?month=2026-05", headers=auth(admin))
    assert "残業(h)" in csv.text and "不足(h)" in csv.text


def test_notifications(client, users, monkeypatch):
    worker, admin = users
    from server import notify

    sent = []
    monkeypatch.setattr(notify, "send_slack",
                        lambda url, text: sent.append(("slack", url, text)))

    # 未設定ではテスト送信は400
    assert client.post("/api/settings/test-notify",
                       headers=auth(admin)).status_code == 400

    # Slack URL を設定 → テスト送信が届く
    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": "https://hooks.slack.test/xxx",
                       "notify_clock": True})
    r = client.post("/api/settings/test-notify", headers=auth(admin))
    assert r.status_code == 200 and r.json()["sent"] == ["slack"]
    assert len(sent) == 1

    # 着席で通知が飛ぶ (notify_clock 有効)
    client.post("/api/clock-in", headers=auth(worker), json={"category": "現場"})
    assert any("着席" in t for _, _, t in sent)
    client.post("/api/clock-out", headers=auth(worker))
    assert any("退席" in t for _, _, t in sent)

    # 秘密情報は監査ログに残さない
    month = __import__("datetime").datetime.now().strftime("%Y-%m")
    audit = client.get(f"/api/reports/audit.csv?month={month}", headers=auth(admin))
    assert "hooks.slack.test" not in audit.text
    assert "slack_webhook_url=***" in audit.text


def test_long_seated_notification(client, users, tmp_path, monkeypatch):
    worker, admin = users
    from server import app as m, notify
    sent = []
    monkeypatch.setattr(notify, "deliver_async",
                        lambda settings, text: sent.append(text))

    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": "https://hooks.slack.test/x",
                       "notify_alert": True, "alert_hours": 6})
    # 7時間前から在席中のセッションを直接投入
    from datetime import datetime, timedelta, timezone
    from server import db
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("INSERT INTO sessions (user_id, clock_in) VALUES (?, ?)",
                     (worker["id"], old))

    with db.get_db(tmp_path / "zatsumu.db") as conn:
        n = m.check_long_seated(conn)
    assert n == 1
    assert any("連続で在席" in t for t in sent)

    # 2回目は重複通知しない
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert m.check_long_seated(conn) == 0


def test_screenshot_owner_can_view(client, users, tmp_path):
    worker, admin = users
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        other = db.create_user(conn, "yamada")

    client.post("/api/clock-in", headers=auth(worker))
    files = {"image": ("s.jpg", b"\xff\xd8fake", "image/jpeg")}
    sid = client.post("/api/screenshots", headers=auth(worker),
                      files=files).json()["screenshot_id"]

    # 本人と管理者は閲覧可、他のメンバーは不可
    assert client.get(f"/api/screenshots/{sid}/image",
                      headers=auth(worker)).status_code == 200
    assert client.get(f"/api/screenshots/{sid}/image",
                      headers=auth(admin)).status_code == 200
    assert client.get(f"/api/screenshots/{sid}/image",
                      headers=auth(other)).status_code == 403


def test_jpeg_helper():
    from PIL import Image
    from client import capture
    img = Image.new("RGB", (2000, 1000), "white")
    data = capture.to_jpeg(img, max_width=1280, blur=2)
    assert data[:2] == b"\xff\xd8"  # JPEG magic
    out = Image.open(__import__("io").BytesIO(data))
    assert out.width == 1280


def _record_session(tmp_path, user_id, clock_in, clock_out):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, clock_in, clock_out) VALUES (?, ?, ?)",
            (user_id, clock_in, clock_out),
        )


def _record_screenshot(tmp_path, user_id, taken_at, *,
                       similarity=None, stall=0, idle=None):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute(
            "INSERT INTO screenshots (user_id, taken_at, path, similarity, stall, idle) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, taken_at, f"{user_id}/x.jpg", similarity, stall, idle),
        )


def test_monthly_report_and_csv(client, users, tmp_path):
    worker, admin = users
    # 2026-05 に2日分、計3時間勤務 (UTC保存。JSTでも同日)
    _record_session(tmp_path, worker["id"], "2026-05-01T09:00:00+00:00",
                    "2026-05-01T11:00:00+00:00")  # 2h
    _record_session(tmp_path, worker["id"], "2026-05-02T09:00:00+00:00",
                    "2026-05-02T10:00:00+00:00")  # 1h
    # 別月は対象外
    _record_session(tmp_path, worker["id"], "2026-04-01T09:00:00+00:00",
                    "2026-04-01T17:00:00+00:00")

    # 一般ユーザーは不可
    assert client.get("/api/reports/monthly?month=2026-05",
                      headers=auth(worker)).status_code == 403

    r = client.get("/api/reports/monthly?month=2026-05", headers=auth(admin))
    assert r.status_code == 200
    rows = {row["name"]: row for row in r.json()["rows"]}
    assert rows["tanaka"]["work_days"] == 2
    assert rows["tanaka"]["total_hours"] == 3.0
    assert rows["boss"]["total_hours"] == 0

    bad = client.get("/api/reports/monthly?month=oops", headers=auth(admin))
    assert bad.status_code == 400

    csv = client.get("/api/reports/monthly.csv?month=2026-05", headers=auth(admin))
    assert csv.status_code == 200
    assert "attachment" in csv.headers["content-disposition"]
    assert "tanaka" in csv.text
    assert "3.0" in csv.text


def test_retention_purge(client, users, tmp_path):
    import io
    from datetime import datetime, timedelta, timezone
    from PIL import Image
    from server import db, retention

    worker, admin = users
    client.post("/api/clock-in", headers=auth(worker))
    img = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(img, "JPEG")

    # 新しいスクショ(残す) と 古いスクショ(消す)
    client.post("/api/screenshots", headers=auth(worker),
                files={"image": ("s.jpg", img.getvalue(), "image/jpeg")})
    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    screenshot_dir = tmp_path / "screenshots"
    old_rel = f"{worker['id']}/old.jpg"
    (screenshot_dir / str(worker["id"])).mkdir(parents=True, exist_ok=True)
    (screenshot_dir / old_rel).write_bytes(img.getvalue())
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute(
            "INSERT INTO screenshots (user_id, taken_at, path) VALUES (?, ?, ?)",
            (worker["id"], old_time, old_rel),
        )

    # 直接呼び出し
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        deleted = retention.purge_old_screenshots(conn, screenshot_dir, 30)
    assert deleted == 1
    assert not (screenshot_dir / old_rel).exists()  # ファイルも消える

    # API 経由(管理者) — もう古いものは無いので0
    r = client.post("/api/admin/purge", headers=auth(admin))
    assert r.status_code == 200
    assert r.json()["deleted"] == 0
    # 一般ユーザーは不可
    assert client.post("/api/admin/purge", headers=auth(worker)).status_code == 403

    # 残ったスクショは1件
    shots = client.get("/api/screenshots", headers=auth(admin)).json()
    assert len(shots) == 1


def test_timezone_day_boundary(client, users, tmp_path):
    """JSTの朝(UTCでは前日深夜)の勤務が正しい日・月に集計されること."""
    worker, admin = users
    # 2026-05-01 07:00-08:00 JST = 2026-04-30 22:00-23:00 UTC
    _record_session(tmp_path, worker["id"], "2026-04-30T22:00:00+00:00",
                    "2026-04-30T23:00:00+00:00")
    r = client.get("/api/reports/monthly?month=2026-05", headers=auth(admin))
    rows = {row["name"]: row for row in r.json()["rows"]}
    assert rows["tanaka"]["total_hours"] == 1.0  # 5月分として計上される
    r = client.get("/api/reports/monthly?month=2026-04", headers=auth(admin))
    rows = {row["name"]: row for row in r.json()["rows"]}
    assert rows["tanaka"]["total_hours"] == 0


def test_user_monthly_detail(client, users, tmp_path):
    worker, admin = users
    # JST 5/1 09:00-12:00 と、日またぎ JST 5/2 23:00 - 5/3 01:00
    _record_session(tmp_path, worker["id"], "2026-05-01T00:00:00+00:00",
                    "2026-05-01T03:00:00+00:00")
    _record_session(tmp_path, worker["id"], "2026-05-02T14:00:00+00:00",
                    "2026-05-02T16:00:00+00:00")

    r = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                   headers=auth(admin))
    assert r.status_code == 200
    data = r.json()
    assert data["user"]["name"] == "tanaka"
    days = {d["date"]: d for d in data["days"]}
    assert days["2026-05-01"]["hours"] == 3.0
    # 日またぎは 5/2 に1時間・5/3 に1時間として分割される
    assert days["2026-05-02"]["hours"] == 1.0
    assert days["2026-05-03"]["hours"] == 1.0

    # 実績画像(タイムライン)に一致率・停滞フラグ・無操作情報が含まれる
    _record_screenshot(tmp_path, worker["id"], "2026-05-01T01:00:00+00:00",
                       similarity=98, stall=5, idle=600)   # 既定 alert_count=3 → 停滞
    _record_screenshot(tmp_path, worker["id"], "2026-05-01T01:05:00+00:00",
                       similarity=20, stall=0, idle=5)
    data2 = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                       headers=auth(admin)).json()
    day1 = next(d for d in data2["days"] if d["date"] == "2026-05-01")
    shots = {s["similarity"]: s for s in day1["screenshots"]}
    assert shots[98]["stalled"] is True and shots[98]["idle_over"] is True
    assert shots[20]["stalled"] is False and shots[20]["idle_over"] is False

    # 一般ユーザーは不可、存在しないユーザーは404
    assert client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                      headers=auth(worker)).status_code == 403
    assert client.get("/api/users/9999/monthly?month=2026-05",
                      headers=auth(admin)).status_code == 404


def test_delete_screenshot(client, users):
    worker, admin = users
    client.post("/api/clock-in", headers=auth(worker))
    files = {"image": ("s.jpg", b"\xff\xd8fake", "image/jpeg")}
    sid = client.post("/api/screenshots", headers=auth(worker),
                      files=files).json()["screenshot_id"]

    # 一般ユーザーには削除権限なし (管理者のみ)
    assert client.delete(f"/api/screenshots/{sid}",
                         headers=auth(worker)).status_code == 403
    assert client.delete(f"/api/screenshots/{sid}",
                         headers=auth(admin)).status_code == 200
    assert client.get(f"/api/screenshots/{sid}/image",
                      headers=auth(admin)).status_code == 404
    assert client.delete(f"/api/screenshots/{sid}",
                         headers=auth(admin)).status_code == 404


def test_sessions_csv(client, users, tmp_path):
    worker, admin = users
    _record_session(tmp_path, worker["id"], "2026-05-01T00:00:00+00:00",
                    "2026-05-01T03:00:00+00:00")
    r = client.get("/api/reports/sessions.csv?month=2026-05", headers=auth(admin))
    assert r.status_code == 200
    assert "tanaka" in r.text
    assert "2026-05-01 09:00:00" in r.text  # JST 表示
    assert "作業区分" in r.text


def test_category_csv_exports(client, users, tmp_path):
    from server import db
    worker, admin = users
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        for ci, co, cat in [
            ("2026-05-01T00:00:00+00:00", "2026-05-01T02:00:00+00:00", "事務作業"),
            ("2026-05-01T03:00:00+00:00", "2026-05-01T04:00:00+00:00", "現場"),
            ("2026-05-02T00:00:00+00:00", "2026-05-02T01:00:00+00:00", "現場"),
        ]:
            conn.execute(
                "INSERT INTO sessions (user_id, clock_in, clock_out, category) "
                "VALUES (?, ?, ?, ?)", (worker["id"], ci, co, cat))
    # 日別×区分 (縦持ち): 日付・メンバー・作業区分・時間
    r = client.get("/api/reports/daily-by-category.csv?month=2026-05",
                   headers=auth(admin))
    assert r.status_code == 200
    body = r.text
    assert "日付" in body and "作業区分" in body and "当日合計(h)" in body
    assert "事務作業" in body and "現場" in body and "tanaka" in body
    assert "2026-05-01" in body and "2026-05-02" in body
    # 5/1 は 事務2h+現場1h=当日合計3.0 が各行の右端に付く
    assert "2026-05-01,tanaka,事務作業,2.0,2:00,3.0" in body
    # 当日合計(h) = その(日,人)の区分別セル(在席時間(h))の和 に一致する
    import csv as _c
    import io as _i
    dg = list(_c.reader(_i.StringIO(body.lstrip("﻿"))))
    hi = {name: dg[0].index(name) for name in ("在席時間(h)", "当日合計(h)")}
    dm = {}
    for gr in dg[1:]:
        if not gr or not gr[0]:
            continue
        dm.setdefault((gr[0], gr[1]), [0.0, float(gr[hi["当日合計(h)"]])])
        dm[(gr[0], gr[1])][0] += float(gr[hi["在席時間(h)"]])
    for (date, name), (cell_sum, day_total) in dm.items():
        assert abs(round(cell_sum, 2) - day_total) < 1e-9
    # 区分別集計(月次マトリクス): メンバー×区分の合計
    r2 = client.get("/api/reports/category.csv?month=2026-05", headers=auth(admin))
    assert r2.status_code == 200
    assert "メンバー" in r2.text and "合計" in r2.text
    assert "事務作業" in r2.text and "現場" in r2.text and "tanaka" in r2.text
    # 列重複なし & 合計=表示セルの和 (丸め順を統一)
    import csv as _csv
    import io as _io
    grid = list(_csv.reader(_io.StringIO(r2.text.lstrip("﻿"))))
    header, ti = grid[0], grid[0].index("合計")
    assert header.count("事務作業") == 1 and header.count("現場") == 1
    for gr in grid[1:]:
        if gr and gr[0]:
            cells = [float(x) for x in gr[1:ti] if x != ""]
            assert abs(sum(cells) - float(gr[ti])) < 1e-9
    # 一般ユーザーは不可
    assert client.get("/api/reports/daily-by-category.csv?month=2026-05",
                      headers=auth(worker)).status_code == 403
    assert client.get("/api/reports/category.csv?month=2026-05",
                      headers=auth(worker)).status_code == 403


def test_clock_in_with_category_and_switch(client, users):
    worker, admin = users
    # 既定の区分一覧
    me = client.get("/api/me", headers=auth(worker)).json()
    assert me["categories"] == ["事務作業", "現場"]
    assert me["category"] is None

    # 区分を指定して着席
    r = client.post("/api/clock-in", headers=auth(worker),
                    json={"category": "現場"})
    assert r.status_code == 200
    assert r.json()["category"] == "現場"
    assert client.get("/api/me", headers=auth(worker)).json()["category"] == "現場"

    # 不明な区分は拒否
    assert client.post("/api/switch-category", headers=auth(worker),
                       json={"category": "宇宙"}).status_code == 400

    # 区分の切り替え: 現在の在席を区切って新区分で続行
    r = client.post("/api/switch-category", headers=auth(worker),
                    json={"category": "事務作業"})
    assert r.status_code == 200
    me = client.get("/api/me", headers=auth(worker)).json()
    assert me["seated"] is True
    assert me["category"] == "事務作業"

    # 同じ区分への切り替えは何もしない
    sid_before = client.post("/api/switch-category", headers=auth(worker),
                             json={"category": "事務作業"})
    assert sid_before.status_code == 200
    assert "session_id" not in sid_before.json()

    # 着席していないと切替不可
    client.post("/api/clock-out", headers=auth(worker))
    assert client.post("/api/switch-category", headers=auth(worker),
                       json={"category": "現場"}).status_code == 409

    # status に区分が出る
    client.post("/api/clock-in", headers=auth(worker), json={"category": "現場"})
    st = {u["name"]: u for u in client.get("/api/status",
                                           headers=auth(admin)).json()}
    assert st["tanaka"]["category"] == "現場"
    assert st["tanaka"]["today_sessions"][-1]["category"] == "現場"


def test_categories_setting_and_breakdown(client, users):
    worker, admin = users
    # 区分の設定変更
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"work_categories": " 開発 , 会議 ,営業 "})
    assert r.status_code == 200
    assert r.json()["work_categories"] == "開発,会議,営業"
    assert client.get("/api/me", headers=auth(worker)).json()["categories"] == \
        ["開発", "会議", "営業"]
    # 空は拒否
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"work_categories": " , "}).status_code == 400

    # 区分別の合計が個人月次に出る (管理者が打刻を区分つきで追加)
    client.post(f"/api/users/{worker['id']}/sessions", headers=auth(admin),
                json={"clock_in": "2026-05-01T09:00", "clock_out": "2026-05-01T12:00",
                      "category": "開発"})
    client.post(f"/api/users/{worker['id']}/sessions", headers=auth(admin),
                json={"clock_in": "2026-05-01T13:00", "clock_out": "2026-05-01T14:00",
                      "category": "会議"})
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-05",
                        headers=auth(admin)).json()
    assert detail["by_category"] == {"開発": 3.0, "会議": 1.0}


# ===================== 自動改善で追加したテスト =====================
from pathlib import Path as _Path

_TPL = _Path(__file__).resolve().parents[1] / "server" / "templates"


def test_settings_redacts_smtp_password(client, users, tmp_path):
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"smtp_host": "smtp.example.com", "mail_to": "b@example.com",
                       "smtp_pass": "s3cret"})
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["smtp_pass"] == ""          # 平文パスワードは返さない
    assert s["smtp_pass_set"] is True     # 設定済みフラグのみ
    # DB には保存されており、通知処理からは参照できる
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert db.get_settings(conn)["smtp_pass"] == "s3cret"
    # パスワードを送らない更新では既存値が保持される
    client.patch("/api/settings", headers=auth(admin), json={"mail_from": "x@e.com"})
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert db.get_settings(conn)["smtp_pass"] == "s3cret"


def test_smtp_port_validation(client, users):
    _, admin = users
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"smtp_port": "abc"}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"smtp_port": "70000"}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"smtp_port": "465"}).status_code == 200


def test_leave_approved_not_clobbered_by_resubmit(client, users):
    worker, admin = users
    client.post("/api/me/leave", headers=auth(worker),
                json={"date": "2026-08-01", "leave_type": "有給休暇", "reason": "A"})
    lid = client.get("/api/leave?status=pending", headers=auth(admin)).json()[0]["id"]
    client.post(f"/api/leave/{lid}/decision", headers=auth(admin),
                json={"approve": True})
    # 承認後の再申請は 409 で拒否され、内容も上書きされない
    assert client.post("/api/me/leave", headers=auth(worker),
                       json={"date": "2026-08-01", "leave_type": "欠勤",
                             "reason": "B"}).status_code == 409
    detail = client.get(f"/api/users/{worker['id']}/monthly?month=2026-08",
                        headers=auth(admin)).json()
    day = {d["date"]: d for d in detail["days"]}["2026-08-01"]
    assert day["leave"] == {"type": "有給休暇", "status": "approved"}


def test_db_indexes_created(client, users, tmp_path):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    for idx in ("idx_sessions_user_open", "idx_sessions_clock_in",
                "idx_screenshots_user_taken", "idx_screenshots_taken",
                "idx_leave_date", "idx_audit_at", "idx_users_team",
                "idx_mails_status", "idx_mails_received"):
        assert idx in names


def test_notify_deliver_payload_and_gating(monkeypatch):
    from server import notify
    sent = []
    monkeypatch.setattr(notify, "send_slack",
                        lambda url, text: sent.append(("slack", url, text)))
    monkeypatch.setattr(notify, "send_email",
                        lambda s, subj, text: sent.append(("email", subj, text)))

    # 未設定なら何も送らない
    assert notify.deliver({}, "hi") == []
    assert sent == []
    # Slack のみ設定
    assert notify.deliver({"slack_webhook_url": "https://h.test/x"}, "着席") == ["slack"]
    assert sent[-1] == ("slack", "https://h.test/x", "着席")
    # Slack + メール両方
    cfg = {"slack_webhook_url": "https://h.test/x", "smtp_host": "smtp.e",
           "mail_to": "a@e.com"}
    assert set(notify.deliver(cfg, "退席")) == {"slack", "email"}
    # 送信失敗チャネルは結果に含めない(例外は握りつぶす)
    monkeypatch.setattr(notify, "send_slack",
                        lambda url, text: (_ for _ in ()).throw(RuntimeError("net")))
    assert notify.deliver({"slack_webhook_url": "https://h.test/x"}, "x") == []


def test_notify_channels_helper():
    from server import notify
    assert notify.channels({}) == []
    assert notify.channels({"slack_webhook_url": "u"}) == ["slack"]
    # メールは host と宛先の両方が必要
    assert notify.channels({"smtp_host": "h"}) == []
    assert notify.channels({"smtp_host": "h", "mail_to": "a@e"}) == ["email"]


def test_templates_escape_user_content():
    """テンプレートがユーザー由来文字列を esc/esc2 で囲んでいる(XSS回帰防止)."""
    admin = (_TPL / "admin.html").read_text(encoding="utf-8")
    member = (_TPL / "member.html").read_text(encoding="utf-8")
    # 強化版エスケープ(引用符も対象)が定義されている
    assert "&quot;" in admin and "&#39;" in admin
    assert "&quot;" in member
    # 代表的なユーザー由来フィールドが esc を通っている
    for token in ("${esc(u.name)}", "${esc(l.name)}", "${esc(l.leave_type)}",
                  "${esc(t.name)}"):
        assert token in admin, token
    # 生挿入が残っていない
    assert "${u.name}" not in admin
    assert "${l.leave_type}" not in admin


# ---- 自動改善フェーズ2で追加したテスト ----

def test_day_segments_helper():
    """tz.day_segments: 日跨ぎ分割・is_open・月境界クランプ."""
    from datetime import datetime, timezone
    from server import tz
    tz.set_tz("Asia/Tokyo")
    start, end = tz.month_window("2026-05")
    now = datetime(2026, 5, 31, tzinfo=timezone.utc)
    # JST 5/2 23:00 - 5/3 01:00 (= UTC 5/2 14:00 - 16:00) → 2区間に分割
    segs = list(tz.day_segments("2026-05-02T14:00:00+00:00",
                                "2026-05-02T16:00:00+00:00", start, end, now))
    assert len(segs) == 2
    assert [s[0].date().isoformat() for s in segs] == ["2026-05-02", "2026-05-03"]
    assert all(s[2] is False for s in segs)        # 退席済みは is_open=False
    # 合計2時間
    total = sum((e - s).total_seconds() for s, e, _ in segs) / 3600
    assert round(total, 2) == 2.0
    # 未退席は末尾区間が is_open=True、now までで打ち切り
    openseg = list(tz.day_segments("2026-05-10T00:00:00+00:00", None,
                                   start, end, datetime(2026, 5, 10, 3, tzinfo=timezone.utc)))
    assert openseg[-1][2] is True


def test_to_jpeg_quality_and_no_resize():
    from PIL import Image
    from client import capture
    big = Image.new("RGB", (400, 200), "white")
    # 元画像が max_width 以下ならリサイズしない
    out = Image.open(__import__("io").BytesIO(
        capture.to_jpeg(big, max_width=1280)))
    assert out.width == 400
    # 同じノイズ画像で低品質 < 高品質 のバイト数
    import random
    noise = Image.new("RGB", (300, 300))
    noise.putdata([(random.randint(0, 255),) * 3 for _ in range(300 * 300)])
    lo = capture.to_jpeg(noise, quality=10)
    hi = capture.to_jpeg(noise, quality=90)
    assert len(lo) < len(hi)


def test_tile_horizontally_lays_out_side_by_side():
    from PIL import Image
    from client import capture
    a = Image.new("RGB", (1920, 1080), "red")
    b = Image.new("RGB", (1280, 1024), "blue")  # 別解像度・別アスペクトでも揃う
    out = capture.tile_horizontally([a, b], gap=8)
    h = min(1080, 1024)  # 共通の高さ = 最小の高さ
    assert out.height == h
    wa = round(1920 * h / 1080)
    wb = round(1280 * h / 1024)
    assert out.width == wa + wb + 8  # 横に並べた幅 + すき間


def test_to_jpeg_widens_cap_by_monitor_count():
    import io
    from PIL import Image
    from client import capture
    # マルチモニター合成(tiles指定)は1モニターあたりの解像度を確保し縮みすぎない
    wide = Image.new("RGB", (3840, 1080), "white")
    out = Image.open(io.BytesIO(capture.to_jpeg(wide, max_width=1280, tiles=3)))
    assert out.width == 3840   # 1280*3 まで許容
    # 縦長2画面を横に並べた合成(width<height*2)も tiles で広い上限になり潰れない
    portrait = Image.new("RGB", (2168, 1920), "white")
    out_p = Image.open(io.BytesIO(capture.to_jpeg(portrait, max_width=1280, tiles=2)))
    assert out_p.width == 2168
    # 単一モニター(tiles=1)は従来どおり max_width に縮小
    normal = Image.new("RGB", (1920, 1080), "white")
    out2 = Image.open(io.BytesIO(capture.to_jpeg(normal, max_width=1280)))
    assert out2.width == 1280


# ---------- 勤務時間の修正申請 ----------
def _add_session(client, admin, worker, ci, co):
    r = client.post(f"/api/users/{worker['id']}/sessions", headers=auth(admin),
                    json={"clock_in": ci, "clock_out": co})
    assert r.status_code == 200
    return r.json()["session_id"]


def _session_times(tmp_path, sid):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        r = conn.execute(
            "SELECT clock_in, clock_out FROM sessions WHERE id=?", (sid,)
        ).fetchone()
    return r["clock_in"], r["clock_out"]


def test_correction_request_list_and_cancel(client, users):
    worker, admin = users
    r = client.post("/api/me/corrections", headers=auth(worker),
                    json={"date": "2026-05-10", "requested_in": "09:00",
                          "requested_out": "18:00", "reason": "打刻忘れ"})
    assert r.status_code == 200
    cid = r.json()["id"]
    lst = client.get("/api/me/corrections", headers=auth(worker)).json()
    assert len(lst) == 1 and lst[0]["status"] == "pending"
    adm = client.get("/api/corrections?status=pending", headers=auth(admin)).json()
    assert any(c["id"] == cid and c["name"] == "tanaka" for c in adm)
    assert client.delete(f"/api/me/corrections/{cid}",
                         headers=auth(worker)).status_code == 200
    assert client.get("/api/me/corrections", headers=auth(worker)).json() == []


def test_correction_validation(client, users):
    worker, _ = users
    assert client.post("/api/me/corrections", headers=auth(worker),
                       json={"date": "2026-05-10"}).status_code == 400
    assert client.post("/api/me/corrections", headers=auth(worker),
                       json={"date": "2026-05-10", "requested_in": "25:99"}
                       ).status_code == 400
    assert client.post("/api/me/corrections", headers=auth(worker),
                       json={"date": "2026-05-10", "requested_in": "18:00",
                             "requested_out": "09:00"}).status_code == 400


def test_correction_approve_updates_session(client, users, tmp_path):
    from server import tz
    worker, admin = users
    sid = _add_session(client, admin, worker, "2026-05-10T09:00", "2026-05-10T12:00")
    cid = client.post("/api/me/corrections", headers=auth(worker),
                      json={"date": "2026-05-10", "requested_out": "18:00"}).json()["id"]
    r = client.post(f"/api/corrections/{cid}/decision", headers=auth(admin),
                    json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    ci, co = _session_times(tmp_path, sid)
    assert tz.local(co).strftime("%H:%M") == "18:00"   # 退席が反映
    assert tz.local(ci).strftime("%H:%M") == "09:00"   # 着席は不変


def test_correction_approve_creates_session_when_none(client, users, tmp_path):
    from server import db, tz
    worker, admin = users
    cid = client.post("/api/me/corrections", headers=auth(worker),
                      json={"date": "2026-05-12", "requested_in": "10:00",
                            "requested_out": "15:00"}).json()["id"]
    assert client.post(f"/api/corrections/{cid}/decision", headers=auth(admin),
                       json={"approve": True}).json()["status"] == "approved"
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        rows = conn.execute(
            "SELECT clock_in, clock_out FROM sessions WHERE user_id=?",
            (worker["id"],)).fetchall()
    assert len(rows) == 1
    assert tz.local(rows[0]["clock_in"]).strftime("%H:%M") == "10:00"
    assert tz.local(rows[0]["clock_out"]).strftime("%H:%M") == "15:00"


def test_correction_reject_keeps_session(client, users, tmp_path):
    worker, admin = users
    sid = _add_session(client, admin, worker, "2026-05-10T09:00", "2026-05-10T12:00")
    before = _session_times(tmp_path, sid)
    cid = client.post("/api/me/corrections", headers=auth(worker),
                      json={"date": "2026-05-10", "requested_out": "18:00"}).json()["id"]
    assert client.post(f"/api/corrections/{cid}/decision", headers=auth(admin),
                       json={"approve": False}).json()["status"] == "rejected"
    assert _session_times(tmp_path, sid) == before


def test_correction_requires_admin(client, users):
    worker, _ = users
    cid = client.post("/api/me/corrections", headers=auth(worker),
                      json={"date": "2026-05-10", "requested_out": "18:00"}).json()["id"]
    assert client.get("/api/corrections", headers=auth(worker)).status_code == 403
    assert client.post(f"/api/corrections/{cid}/decision", headers=auth(worker),
                       json={"approve": True}).status_code == 403


# ---------- スタッフ個別の通知ON/OFF ----------
def test_per_user_notify_toggle_and_gate(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    calls = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: calls.append(text))
    # 全社の着席/退席通知を有効化
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"notify_clock": True}).status_code == 200
    # 一覧・PATCH に notify_enabled が含まれる
    lst = client.get("/api/users", headers=auth(admin)).json()
    assert all("notify_enabled" in u for u in lst)
    # 既定(通知ON)では着席・退席で通知される
    client.post("/api/clock-in", headers=auth(worker))
    client.post("/api/clock-out", headers=auth(worker))
    assert len(calls) == 2
    # このスタッフの通知をOFFにすると通知されない
    r = client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                     json={"notify_enabled": False})
    assert r.status_code == 200 and r.json()["notify_enabled"] == 0
    calls.clear()
    client.post("/api/clock-in", headers=auth(worker))
    client.post("/api/clock-out", headers=auth(worker))
    assert calls == []


def test_member_email_register_and_used_as_recipient(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    captured = []
    monkeypatch.setattr(app_module.notify, "deliver_async",
                        lambda settings, text: captured.append(dict(settings)))
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"notify_clock": True}).status_code == 200
    # メールアドレスを登録
    r = client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                     json={"email": "tanaka@example.com"})
    assert r.status_code == 200 and r.json()["email"] == "tanaka@example.com"
    # 一覧にも反映
    lst = client.get("/api/users", headers=auth(admin)).json()
    assert any(u["email"] == "tanaka@example.com" for u in lst)
    # 着席通知の宛先に本人メールが含まれる
    client.post("/api/clock-in", headers=auth(worker))
    assert captured and "tanaka@example.com" in captured[-1]["mail_to"]


def test_member_email_invalid_rejected(client, users):
    worker, admin = users
    assert client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                        json={"email": "not-an-email"}).status_code == 400


def test_list_users_includes_token(client, users):
    worker, admin = users
    by = {u["name"]: u for u in
          client.get("/api/users", headers=auth(admin)).json()}
    assert by["tanaka"]["token"] == worker["token"]


def test_decide_leave_guards_double_decision(client, users):
    worker, admin = users
    client.post("/api/me/leave", headers=auth(worker),
                json={"date": "2026-05-10", "leave_type": "有給休暇"})
    lid = client.get("/api/me/leave", headers=auth(worker)).json()[0]["id"]
    assert client.post(f"/api/leave/{lid}/decision", headers=auth(admin),
                       json={"approve": True}).status_code == 200
    # 既に処理済みの再決定は 409 で拒否(承認済みを却下に翻せない)
    assert client.post(f"/api/leave/{lid}/decision", headers=auth(admin),
                       json={"approve": False}).status_code == 409


def test_screenshot_upload_rejects_non_jpeg_and_oversize(client, users):
    worker, _ = users
    client.post("/api/clock-in", headers=auth(worker))
    # 非JPEG → 415
    assert client.post("/api/screenshots", headers=auth(worker),
                       files={"image": ("x.png", b"\x89PNG\r\n", "image/png")}
                       ).status_code == 415
    # サイズ超過 → 413
    big = b"\xff\xd8" + b"0" * 6_000_001
    assert client.post("/api/screenshots", headers=auth(worker),
                       files={"image": ("x.jpg", big, "image/jpeg")}
                       ).status_code == 413
    # 正常な(小さい)JPEG → 200
    assert client.post("/api/screenshots", headers=auth(worker),
                       files={"image": ("x.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")}
                       ).status_code == 200


def test_send_email_builds_mime(monkeypatch):
    from server import notify

    class FakeSMTP:
        last = None

        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port
            self.tls = False; self.logged = None; self.sent = None
            FakeSMTP.last = self

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): self.tls = True
        def login(self, u, p): self.logged = (u, p)
        def sendmail(self, frm, to, msg): self.sent = (frm, to, msg)

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    cfg = {"smtp_host": "smtp.e", "smtp_port": "587", "smtp_user": "u@e",
           "smtp_pass": "pw", "mail_from": "from@e", "mail_to": "a@e.com, b@e.com"}
    notify.send_email(cfg, "件名X", "本文Y")
    f = FakeSMTP.last
    assert (f.host, f.port) == ("smtp.e", 587)
    assert f.tls is True and f.logged == ("u@e", "pw")
    frm, to, raw = f.sent
    assert frm == "from@e"
    assert to == ["a@e.com", "b@e.com"]          # 複数宛先を分割
    assert "from@e" in raw and "a@e.com" in raw
    # mail_from 未設定なら smtp_user が差出人になる
    notify.send_email({**cfg, "mail_from": ""}, "s", "t")
    assert FakeSMTP.last.sent[0] == "u@e"


def test_migrate_idempotent(tmp_path):
    from server import db
    p = tmp_path / "mig.db"
    db.connect(p).close()
    db.connect(p).close()  # 2回目もエラーにならない(ALTERはガード済み)
    conn = db.connect(p)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    assert cols.count("team_id") == 1 and cols.count("active") == 1
    shcols = [r["name"] for r in conn.execute("PRAGMA table_info(screenshots)")]
    assert {"sig", "similarity", "stall"} <= set(shcols)
    conn.close()


def _jpeg(color=(123, 200, 50), size=(160, 120)):
    """テスト用のデコード可能な JPEG バイト列を作る."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _composite_jpeg(colors, size=(120, 90), gap=8):
    """複数モニターを横連結した 1 枚の JPEG (colors: モニターごとの色)."""
    import io
    from PIL import Image
    parts = [Image.new("RGB", size, c) for c in colors]
    w = sum(p.width for p in parts) + gap * (len(parts) - 1)
    canvas = Image.new("RGB", (w, size[1]), (17, 17, 27))
    x = 0
    for p in parts:
        canvas.paste(p, (x, 0))
        x += p.width + gap
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def test_imaging_per_monitor_min_not_diluted():
    from server import imaging
    g, g2 = (200, 200, 200), (120, 120, 120)
    A = _composite_jpeg([g, g, g, g])
    B = _composite_jpeg([g, g, g, g2])   # 4台中1台だけ変化
    # 全体一括(従来 tiles=1)だと変化が薄まり高い一致率に見える
    whole = imaging.similarity(imaging.signature(A, 1), imaging.signature(B, 1))
    # モニター別(tiles=4)は最も動いたモニターで判定 → 低い一致率
    per = imaging.similarity(imaging.signature(A, 4), imaging.signature(B, 4))
    assert per < whole          # 希釈されない
    assert whole > 90           # 一括だと「ほぼ同じ」=停滞に誤判定しやすい
    assert per < 85             # モニター別なら 1 台の変化をちゃんと検知
    # 全モニター同一なら ~100
    assert imaging.similarity(imaging.signature(A, 4),
                              imaging.signature(A, 4)) >= 99
    # 指紋長はモニター枚数に比例 (256バイト/台)
    assert len(bytes.fromhex(imaging.signature(A, 4))) == 256 * 4
    # 枚数(指紋長)が違えば比較不能 → None (レイアウト変更扱い)
    assert imaging.similarity(imaging.signature(A, 4),
                              imaging.signature(A, 2)) is None


def test_stall_per_monitor_via_upload(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    alerts = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: alerts.append(text))
    client.patch("/api/settings", headers=auth(admin),
                 json={"notify_stall": True, "stall_threshold": 90,
                       "stall_alert_count": 1})
    client.post("/api/clock-in", headers=auth(worker))
    g, g2 = (200, 200, 200), (120, 120, 120)
    A = _composite_jpeg([g, g, g])
    B = _composite_jpeg([g, g, g2])   # 3台中1台だけ変化
    up = lambda data, t: client.post(
        "/api/screenshots", headers=auth(worker),
        files={"image": ("s.jpg", data, "image/jpeg")}, data={"tiles": str(t)})
    assert up(A, 3).status_code == 200
    assert up(B, 3).status_code == 200   # 1台動いた → 停滞ではない(通知なし)
    assert alerts == []
    # 全モニター静止が続けば停滞として通知される
    assert up(B, 3).status_code == 200   # 直前と完全一致
    assert len(alerts) == 1


def test_imaging_signature_and_similarity():
    from server import imaging
    a, b, c = _jpeg((255, 255, 255)), _jpeg((255, 255, 255)), _jpeg((0, 0, 0))
    sa, sb, sc = imaging.signature(a), imaging.signature(b), imaging.signature(c)
    assert sa and sb and sc
    assert imaging.similarity(sa, sb) >= 99      # 同じ画像はほぼ一致
    assert imaging.similarity(sa, sc) < 10       # 白と黒は大きく異なる
    # 壊れた画像・比較不能は None
    assert imaging.signature(b"\xff\xd8not-a-jpeg") is None
    assert imaging.similarity("zz", sa) is None   # 不正な hex
    assert imaging.similarity(sa, None) is None


def test_screen_stall_alert_flow(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    alerts = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: alerts.append(text))
    # 2回連続で一致したら通知する設定にする
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"notify_stall": True, "stall_threshold": 90,
                              "stall_alert_count": 2}).status_code == 200
    client.post("/api/clock-in", headers=auth(worker))

    same = _jpeg((30, 60, 120))
    up = lambda b: client.post("/api/screenshots", headers=auth(worker),
                               files={"image": ("s.jpg", b, "image/jpeg")})
    assert up(same).status_code == 200      # 1枚目: 比較対象なし stall=0
    assert up(same).status_code == 200      # 2枚目: 一致 stall=1 (通知なし)
    assert len(alerts) == 0
    assert up(same).status_code == 200      # 3枚目: stall=2 → しきい値到達で通知
    assert len(alerts) == 1
    assert "変化していません" in alerts[0]

    # 同じ画面が続いても連投しない(到達した瞬間のみ)
    assert up(same).status_code == 200
    assert len(alerts) == 1

    # 実績画像(一覧API)にも1枚ごとの一致率・停滞フラグが付く
    shots = client.get(f"/api/screenshots?user_id={worker['id']}",
                       headers=auth(admin)).json()
    assert all("stalled" in s and "idle_over" in s for s in shots)
    assert any(s["stalled"] for s in shots)                 # stall>=2 の画像がある
    assert any((s.get("similarity") or 0) >= 90 for s in shots)

    # 停滞中はステータス一覧に stalled フラグが立つ
    st = {u["name"]: u for u in
          client.get("/api/status", headers=auth(admin)).json()}
    assert st["tanaka"]["stalled"] is True

    # 画面が変われば stall はリセットされる(明度が大きく異なる画像)
    assert up(_jpeg((255, 255, 255))).status_code == 200
    assert len(alerts) == 1
    st = {u["name"]: u for u in
          client.get("/api/status", headers=auth(admin)).json()}
    assert st["tanaka"]["stalled"] is False

    # 一覧APIに similarity/stall が載り、sig は除外される
    shots = client.get(f"/api/screenshots?user_id={worker['id']}",
                       headers=auth(admin)).json()
    latest = shots[0]
    assert "sig" not in latest
    assert latest["stall"] == 0 and latest["similarity"] is not None


def test_stall_disabled_or_low_match_no_alert(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    alerts = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: alerts.append(text))
    client.post("/api/clock-in", headers=auth(worker))
    up = lambda b: client.post("/api/screenshots", headers=auth(worker),
                               files={"image": ("s.jpg", b, "image/jpeg")})
    # 停滞通知OFFなら、同じ画面が続いても通知しない
    client.patch("/api/settings", headers=auth(admin),
                 json={"notify_stall": False, "stall_alert_count": 1})
    for _ in range(4):
        up(_jpeg((10, 20, 30)))
    assert alerts == []
    # ONでも毎回画面が変われば一致せず通知されない
    client.patch("/api/settings", headers=auth(admin),
                 json={"notify_stall": True, "stall_threshold": 95,
                       "stall_alert_count": 1})
    up(_jpeg((0, 0, 0)))
    up(_jpeg((255, 255, 255)))
    up(_jpeg((0, 0, 0)))
    assert alerts == []


def test_stall_settings_validation(client, users):
    worker, admin = users
    # 範囲外は 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"stall_threshold": 40}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"stall_alert_count": 0}).status_code == 400
    # 正常値は反映される
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"stall_threshold": 98, "stall_alert_count": 5,
                              "notify_stall": False}).status_code == 200
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["stall_threshold"] == 98 and s["stall_alert_count"] == 5
    assert s["notify_stall"] == 0


def test_demo_seed_idempotent(tmp_path):
    from server import db, demo
    shots = tmp_path / "shots"
    with db.get_db(tmp_path / "demo.db") as conn:
        assert demo.seed(conn, shots) is True       # 空DBには投入
    with db.get_db(tmp_path / "demo.db") as conn:
        assert demo.seed(conn, shots) is False      # 2回目はスキップ
        # ユーザーが重複していない
        names = [r["name"] for r in conn.execute("SELECT name FROM users")]
        assert len(names) == len(set(names))


def test_long_seated_alert_once_per_session(client, users, tmp_path, monkeypatch):
    worker, admin = users
    from server import app as m, notify, db
    from datetime import datetime, timedelta, timezone
    sent = []
    monkeypatch.setattr(notify, "deliver_async", lambda s, t: sent.append(t))
    client.patch("/api/settings", headers=auth(admin),
                 json={"slack_webhook_url": "https://h.test/x",
                       "notify_alert": True, "alert_hours": 6})
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("INSERT INTO sessions (user_id, clock_in) VALUES (?, ?)",
                     (worker["id"], old))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert m.check_long_seated(conn) == 1       # 初回通知
        assert m.check_long_seated(conn) == 0       # 同一セッションは再通知しない
    # 退席→再着席した新セッションは再び対象になりうる(alert_notified=0)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE sessions SET clock_out = ? WHERE user_id = ?",
                     (datetime.now(timezone.utc).isoformat(), worker["id"]))
        conn.execute("INSERT INTO sessions (user_id, clock_in) VALUES (?, ?)",
                     (worker["id"], old))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert m.check_long_seated(conn) == 1
    assert len(sent) == 2


def test_background_loop_survives_errors_and_cancels(client, users, monkeypatch):
    """_background_loop は1回の失敗で死なず、cancel では速やかに終了する."""
    import asyncio
    from server import app as m

    # この実装が依拠する不変条件: CancelledError は Exception では捕捉されない
    assert not issubclass(asyncio.CancelledError, Exception)

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("DB down")

    monkeypatch.setattr(m.db, "connect", boom)
    monkeypatch.setattr(m, "ALERT_CHECK_INTERVAL_MIN", 0.0001)  # ほぼ即時に反復

    async def run():
        task = asyncio.create_task(m._background_loop())
        await asyncio.sleep(0.05)            # 数回反復させる(毎回失敗)
        assert not task.done()               # 失敗してもループは生存
        assert calls["n"] >= 1               # 実際に失敗を踏んでいる
        task.cancel()                        # cancel で確実に終了する
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled() or task.done()

    asyncio.run(run())


def test_idle_and_activity_rate(client, users):
    worker, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"idle_threshold": 120})
    client.post("/api/clock-in", headers=auth(worker))
    j = _jpeg()
    up = lambda idle: client.post(
        "/api/screenshots", headers=auth(worker),
        files={"image": ("s.jpg", j, "image/jpeg")}, data={"idle": str(idle)})
    assert up(10).status_code == 200    # 稼働 (idle<120)
    assert up(20).status_code == 200    # 稼働
    assert up(300).status_code == 200   # 非稼働 (idle>=120)
    st = {u["name"]: u for u in
          client.get("/api/status", headers=auth(admin)).json()}
    assert st["tanaka"]["activity"] == 67   # 3枚中2枚が稼働 → round(2/3*100)
    # idle未送信(Web等)の人は計測対象外 → activity は null
    assert st["boss"]["activity"] is None


def test_clockout_and_idle_settings_validation(client, users):
    worker, admin = users
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"clockout_reminder_time": "25:99"}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"idle_threshold": 5}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"clockout_reminder": False,
                              "clockout_reminder_time": "19:30",
                              "idle_threshold": 180}).status_code == 200
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["clockout_reminder"] == 0
    assert s["clockout_reminder_time"] == "19:30"
    assert s["idle_threshold"] == 180


def test_clockout_reminder_flow(client, users, monkeypatch, tmp_path):
    from server import app as app_module, db
    worker, admin = users
    sent = []
    monkeypatch.setattr(app_module.notify, "deliver_async",
                        lambda settings, text: sent.append(text))
    client.patch(f"/api/users/{worker['id']}", headers=auth(admin),
                 json={"email": "w@e.com"})
    # 時刻00:00 = 常に過ぎている。ON にする
    client.patch("/api/settings", headers=auth(admin),
                 json={"clockout_reminder": True, "clockout_reminder_time": "00:00"})
    client.post("/api/clock-in", headers=auth(worker))   # 未退勤セッション

    def check():
        conn = db.connect(tmp_path / "zatsumu.db")
        try:
            return app_module.check_clockout_reminders(conn)
        finally:
            conn.close()

    assert check() == 1
    assert any("退勤" in t and "tanaka" in t for t in sent)
    sent.clear()
    assert check() == 0           # 2回目は重複通知しない
    assert sent == []
    # OFF なら対象でも通知しない
    client.patch("/api/settings", headers=auth(admin),
                 json={"clockout_reminder": False})
    client.post("/api/clock-out", headers=auth(worker))
    client.post("/api/clock-in", headers=auth(worker))   # 新しい未退勤セッション
    assert check() == 0
    assert sent == []


def test_monthly_report_includes_activity(client, users):
    worker, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"idle_threshold": 120})
    client.post("/api/clock-in", headers=auth(worker))
    j = _jpeg()
    up = lambda idle: client.post(
        "/api/screenshots", headers=auth(worker),
        files={"image": ("s.jpg", j, "image/jpeg")}, data={"idle": str(idle)})
    up(10); up(10); up(500)   # 3枚中2枚が稼働 → 67%
    rep = client.get("/api/reports/monthly", headers=auth(admin)).json()
    by = {r["name"]: r for r in rep["rows"]}
    assert by["tanaka"]["activity"] == 67
    assert by["boss"]["activity"] is None   # idle計測なしは null


def test_reports_summary_and_payroll(client, users):
    worker, admin = users
    # 当月にセッションを作る(着席→退席)
    import datetime as _dt
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    month = _dt.datetime.now().strftime("%Y-%m")
    client.post(f"/api/users/{worker['id']}/sessions", headers=auth(admin),
                json={"clock_in": f"{today}T09:00", "clock_out": f"{today}T19:30",
                      "category": "事務作業"})
    # 集計サマリ: 日別/区分別/チーム別
    s = client.get(f"/api/reports/summary?month={month}", headers=auth(admin)).json()
    assert any(d["hours"] > 0 for d in s["daily"])
    assert s["by_category"].get("事務作業", 0) > 0
    assert any(t["team"] == "未所属" and t["hours"] > 0 for t in s["by_team"])
    # 給与用CSV: 時:分 併記 (10.5h → 10:30)
    csv = client.get(f"/api/reports/payroll.csv?month={month}", headers=auth(admin))
    assert csv.status_code == 200
    assert "tanaka" in csv.text and "10:30" in csv.text
    # 権限: 一般ユーザーは不可
    assert client.get(f"/api/reports/summary?month={month}",
                      headers=auth(worker)).status_code == 403


def test_test_email_endpoint(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    # SMTP未設定 → 400
    assert client.post("/api/settings/test-email", headers=auth(admin),
                       json={"to": "x@e.com"}).status_code == 400
    client.patch("/api/settings", headers=auth(admin),
                 json={"smtp_host": "smtp.e", "smtp_user": "u@e", "mail_from": "from@e"})
    # 不正アドレス → 400
    assert client.post("/api/settings/test-email", headers=auth(admin),
                       json={"to": "not-email"}).status_code == 400
    sent = {}
    monkeypatch.setattr(app_module.notify, "send_email",
                        lambda settings, subject, text: sent.update(
                            to=settings["mail_to"]))
    r = client.post("/api/settings/test-email", headers=auth(admin),
                    json={"to": "  taro@e.com  "})
    assert r.status_code == 200 and r.json()["sent"] == "taro@e.com"
    assert sent["to"] == "taro@e.com"   # 宛先がこのアドレスに差し替わっている

    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(app_module.notify, "send_email", boom)
    assert client.post("/api/settings/test-email", headers=auth(admin),
                       json={"to": "taro@e.com"}).status_code == 502
    # 一般ユーザーは不可
    assert client.post("/api/settings/test-email", headers=auth(worker),
                       json={"to": "taro@e.com"}).status_code == 403


def test_staff_cannot_see_other_staff(client, tmp_path):
    """スタッフ(非管理者)は他人のデータを一切見れない・管理APIも叩けない."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        a = db.create_user(conn, "alice")          # 非管理者
        b = db.create_user(conn, "bob")            # 非管理者
        admin = db.create_user(conn, "boss", is_admin=True)
    # alice が着席→スクショ→日報
    client.post("/api/clock-in", headers=auth(a))
    sid = client.post("/api/screenshots", headers=auth(a),
                      files={"image": ("s.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")}
                      ).json()["screenshot_id"]
    client.put("/api/me/journal", headers=auth(a), json={"body": "aliceの日報"})

    H = auth(b)   # bob のトークン
    # 他人のスクショ画像は 403
    assert client.get(f"/api/screenshots/{sid}/image", headers=H).status_code == 403
    # 全員分・他人分を返す/操作する管理APIは全部 403
    for path in ["/api/status", "/api/users", "/api/teams", "/api/journals",
                 "/api/leave", "/api/corrections", "/api/screenshots",
                 "/api/reports/monthly", "/api/reports/summary", "/api/settings",
                 "/api/mail",
                 f"/api/users/{a['id']}/monthly", f"/api/users/{a['id']}/journal"]:
        assert client.get(path, headers=H).status_code == 403, path
    # 他人を操作する系も 403
    assert client.post(f"/api/users/{a['id']}/clock-out", headers=H).status_code == 403
    assert client.post("/api/users", headers=H, json={"name": "x"}).status_code == 403
    # bob 自身のページは自分のデータだけ (alice の日報は出ない)
    assert client.get("/api/me/journal", headers=H).json()["body"] == ""
    assert client.get("/api/me/monthly", headers=H).status_code == 200
    # bob は自分のスクショは見れる / 管理者は alice のスクショを見れる
    client.post("/api/clock-in", headers=H)
    bsid = client.post("/api/screenshots", headers=H,
                       files={"image": ("s.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")}
                       ).json()["screenshot_id"]
    assert client.get(f"/api/screenshots/{bsid}/image", headers=H).status_code == 200
    assert client.get(f"/api/screenshots/{sid}/image",
                      headers=auth(admin)).status_code == 200
    # 無効トークンは 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_stall_alert_suppressed_when_user_active(client, users, monkeypatch):
    """操作中(idleが閾値未満=在席)なら、画面が静止していても停滞アラートを出さない."""
    from server import app as app_module
    worker, admin = users
    alerts = []
    monkeypatch.setattr(app_module, "_notify",
                        lambda conn, text, member_email=None: alerts.append(text))
    client.patch("/api/settings", headers=auth(admin),
                 json={"notify_stall": True, "stall_threshold": 90,
                       "stall_alert_count": 1, "idle_threshold": 120})
    client.post("/api/clock-in", headers=auth(worker))
    img1, img2 = _jpeg((20, 40, 80)), _jpeg((210, 60, 60))
    up = lambda img, idle: client.post(
        "/api/screenshots", headers=auth(worker),
        files={"image": ("s.jpg", img, "image/jpeg")}, data={"idle": str(idle)})
    up(img1, 5)            # 1枚目
    up(img1, 5)            # 画面同一→stall=1 だが idle=5(操作中) → 通知しない
    assert alerts == []
    up(img2, 300)          # 別画面でstallリセット
    up(img2, 300)          # 画面同一→stall=1 かつ idle=300(離席) → 通知する
    assert len(alerts) == 1
    assert "変化していません" in alerts[0]


def test_delete_user_removes_all_data(client, users, tmp_path):
    from server import db
    import glob
    worker, admin = users
    # worker のデータ(打刻/スクショ/日報/休暇)を作る
    client.post("/api/clock-in", headers=auth(worker))
    client.post("/api/screenshots", headers=auth(worker),
                files={"image": ("s.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")})
    client.post("/api/clock-out", headers=auth(worker))
    client.put("/api/me/journal", headers=auth(worker), json={"body": "x"})
    client.post("/api/me/leave", headers=auth(worker),
                json={"date": "2026-06-10", "leave_type": "有給休暇"})
    sdir = str(tmp_path / "screenshots" / str(worker["id"]))
    assert glob.glob(sdir + "/*.jpg")          # 画像ファイルがある
    # 権限・自己削除ガード
    assert client.delete(f"/api/users/{admin['id']}",
                         headers=auth(worker)).status_code == 403
    assert client.delete(f"/api/users/{admin['id']}",
                         headers=auth(admin)).status_code == 400
    # 削除実行
    assert client.delete(f"/api/users/{worker['id']}",
                         headers=auth(admin)).status_code == 200
    # ユーザーと関連データが全消去
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id=?",
                            (worker["id"],)).fetchone() is None
        for tbl in ("sessions", "screenshots", "journals",
                    "leave_requests", "corrections"):
            assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE user_id=?",
                                (worker["id"],)).fetchone()[0] == 0
    assert glob.glob(sdir + "/*.jpg") == []    # 画像ファイルも消えた
    # 旧トークンは無効・存在しないIDは404
    assert client.get("/api/me", headers=auth(worker)).status_code == 401
    assert client.delete(f"/api/users/{worker['id']}",
                         headers=auth(admin)).status_code == 404


def test_download_client(client, tmp_path):
    # 未配置 → 404
    assert client.get("/download/client").status_code == 404
    # 配置すると認証なしでDLできる(ログイン画面から取得するため)
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "勤怠管理.exe").write_bytes(b"MZ-fake-exe-bytes")
    r = client.get("/download/client")
    assert r.status_code == 200
    assert r.content == b"MZ-fake-exe-bytes"
    assert "attachment" in r.headers.get("content-disposition", "")
    # zip(フォルダ版)があれば exe より優先して配信する
    (d / "勤怠管理.zip").write_bytes(b"PK-fake-zip-bytes")
    r = client.get("/download/client")
    assert r.status_code == 200
    assert r.content == b"PK-fake-zip-bytes"
    assert "application/zip" in r.headers.get("content-type", "")


# ===================== メール (受信箱・優先度・AI返信) =====================

def _add_mail(tmp_path, n=1, **kw):
    """テスト用に受信メールを1件直接投入して id を返す."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        cur = conn.execute(
            "INSERT INTO mails (message_id, from_addr, from_name, subject, body,"
            " received_at, fetched_at, priority, status, draft_reply,"
            " references_hdr) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kw.get("message_id", f"<m{n}@example.com>"),
             kw.get("from_addr", "taro@example.com"),
             kw.get("from_name", "太郎"),
             kw.get("subject", f"件名{n}"),
             kw.get("body", "本文"),
             kw.get("received_at", f"2026-08-{10 + n:02d}T00:00:00+00:00"),
             kw.get("fetched_at",
                    kw.get("received_at", f"2026-08-{10 + n:02d}T00:00:00+00:00")),
             kw.get("priority", 2),
             kw.get("status", "unhandled"),
             kw.get("draft_reply", ""),
             kw.get("references_hdr", "")))
        return cur.lastrowid


def test_mail_rule_priority():
    from server import mail
    s = {"mail_vip_addresses": "boss@client.com",
         "mail_urgent_keywords": "至急,緊急,クレーム"}
    base = {"from_addr": "a@e.com", "from_name": "A", "body": ""}
    # 緊急キーワード → 高
    p, reason = mail.rule_priority(s, {**base, "subject": "【至急】確認のお願い"})
    assert p == 1 and "至急" in reason
    # VIP差出人 → 高
    p, _ = mail.rule_priority(
        s, {**base, "from_addr": "boss@client.com", "subject": "定例の件"})
    assert p == 1
    # 自動配信 → 低
    p, _ = mail.rule_priority(
        s, {**base, "from_addr": "no-reply@news.com", "subject": "お知らせ"})
    assert p == 3
    p, _ = mail.rule_priority(
        s, {**base, "subject": "メルマガ 8月号", "body": "配信停止はこちら"})
    assert p == 3
    # それ以外 → 中
    p, _ = mail.rule_priority(s, {**base, "subject": "打ち合わせ日程の相談"})
    assert p == 2


def test_mail_classify_falls_back_to_rule_without_api_key():
    from server import mail
    s = {"anthropic_api_key": "", "mail_vip_addresses": "",
         "mail_urgent_keywords": "至急"}
    p, reason, source = mail.classify_priority(
        s, {"from_addr": "a@e.com", "from_name": "", "subject": "至急お願い",
            "body": ""})
    assert (p, source) == (1, "rule") and reason


def test_mail_template_reply_and_signature():
    from server import mail
    s = {"anthropic_api_key": "", "company_name": "テスト商事",
         "mail_signature": "――\nテスト商事 総務部"}
    m = {"from_addr": "taro@example.com", "from_name": "太郎",
         "subject": "見積もりの件", "body": "よろしくお願いします"}
    body, source = mail.generate_reply(s, m)
    assert source == "template"
    assert "太郎 様" in body and "見積もりの件" in body
    assert body.endswith("テスト商事 総務部")   # 署名が末尾に付く
    # reply_subject は Re: を付ける (既に付いていれば二重にしない)
    assert mail.reply_subject("見積もりの件") == "Re: 見積もりの件"
    assert mail.reply_subject("Re: 見積もりの件") == "Re: 見積もりの件"
    assert mail.reply_subject("") == "Re: (件名なし)"


def test_mail_parse_message():
    from email.message import EmailMessage
    from server import mail
    msg = EmailMessage()
    msg["From"] = "山田 <yamada@example.com>"
    msg["To"] = "info@example.com"
    msg["Subject"] = "【至急】テストの件"
    msg["Date"] = "Mon, 24 Aug 2026 10:00:00 +0900"
    msg["Message-ID"] = "<abc123@example.com>"
    msg.set_content("本文です。\nよろしくお願いします。")
    p = mail.parse_message(msg.as_bytes())
    assert p["message_id"] == "<abc123@example.com>"
    assert p["from_addr"] == "yamada@example.com"
    assert p["from_name"] == "山田"
    assert p["subject"] == "【至急】テストの件"
    assert "本文です。" in p["body"]
    assert p["received_at"] == "2026-08-24T01:00:00+00:00"  # UTC ISO に正規化
    # Message-ID が無いメールでも内容ハッシュで重複判定用IDが付く
    msg2 = EmailMessage()
    msg2["From"] = "x@example.com"
    msg2["Subject"] = "id無し"
    msg2.set_content("a")
    p2 = mail.parse_message(msg2.as_bytes())
    assert p2["message_id"].startswith("<zatsumu-")


def test_mail_parse_html_only_body():
    from email.message import EmailMessage
    from server import mail
    msg = EmailMessage()
    msg["From"] = "h@example.com"
    msg["Subject"] = "html"
    msg.set_content("<p>こんにちは<br>世界</p><script>evil()</script>",
                    subtype="html")
    p = mail.parse_message(msg.as_bytes())
    assert "こんにちは" in p["body"] and "世界" in p["body"]
    assert "<p>" not in p["body"] and "evil" not in p["body"]


def test_mail_list_sort_and_filters(client, users, tmp_path):
    _, admin = users
    mid_low = _add_mail(tmp_path, n=1, priority=3)
    mid_high = _add_mail(tmp_path, n=2, priority=1)
    mid_mid_old = _add_mail(tmp_path, n=3, priority=2,
                            received_at="2026-08-01T00:00:00+00:00")
    mid_mid_new = _add_mail(tmp_path, n=4, priority=2,
                            received_at="2026-08-20T00:00:00+00:00")
    mid_replied = _add_mail(tmp_path, n=5, priority=2, status="replied")
    rows = client.get("/api/mail", headers=auth(admin)).json()
    # 優先度[高]が先頭・同一優先度は新しい順
    assert [r["id"] for r in rows] == [
        mid_high, mid_mid_new, mid_replied, mid_mid_old, mid_low]
    # status フィルタ
    ids = {r["id"] for r in
           client.get("/api/mail?status=unhandled", headers=auth(admin)).json()}
    assert mid_replied not in ids and mid_high in ids
    # priority フィルタ
    rows = client.get("/api/mail?priority=1", headers=auth(admin)).json()
    assert [r["id"] for r in rows] == [mid_high]
    # 不正な値は 400
    assert client.get("/api/mail?status=bogus",
                      headers=auth(admin)).status_code == 400
    assert client.get("/api/mail?priority=9",
                      headers=auth(admin)).status_code == 400
    # 一覧は本文を含まない(要約のみ)・認証必須
    assert "body" not in client.get(
        "/api/mail", headers=auth(admin)).json()[0]
    assert client.get("/api/mail").status_code == 401


def test_mail_detail_and_patch(client, users, tmp_path):
    worker, admin = users
    mid = _add_mail(tmp_path, body="こんにちは")
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["body"] == "こんにちは" and d["status"] == "unhandled"
    assert client.get("/api/mail/9999", headers=auth(admin)).status_code == 404
    assert client.get(f"/api/mail/{mid}", headers=auth(worker)).status_code == 403
    # 下書きの保存
    r = client.patch(f"/api/mail/{mid}", headers=auth(admin),
                     json={"draft_reply": "編集済みの下書き"})
    assert r.status_code == 200
    assert client.get(f"/api/mail/{mid}",
                      headers=auth(admin)).json()["draft_reply"] == "編集済みの下書き"
    # 優先度の手動変更 → priority_source が manual になる
    client.patch(f"/api/mail/{mid}", headers=auth(admin), json={"priority": 1})
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["priority"] == 1 and d["priority_source"] == "manual"
    # 状態変更 (対応不要)
    client.patch(f"/api/mail/{mid}", headers=auth(admin),
                 json={"status": "archived"})
    assert client.get(f"/api/mail/{mid}",
                      headers=auth(admin)).json()["status"] == "archived"
    # バリデーション
    assert client.patch(f"/api/mail/{mid}", headers=auth(admin),
                        json={}).status_code == 400
    assert client.patch(f"/api/mail/{mid}", headers=auth(admin),
                        json={"priority": 9}).status_code == 400
    assert client.patch(f"/api/mail/{mid}", headers=auth(admin),
                        json={"status": "bogus"}).status_code == 400


def test_mail_generate_draft_endpoint(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    _, admin = users
    mid = _add_mail(tmp_path)
    calls = []

    def fake_generate(settings, mail_row, instructions=""):
        calls.append(instructions)
        return "生成した返信文", "ai"

    monkeypatch.setattr(app_module.mail, "generate_reply", fake_generate)
    r = client.post(f"/api/mail/{mid}/draft", headers=auth(admin),
                    json={"instructions": "丁寧に断る"})
    assert r.status_code == 200
    assert r.json() == {"draft_reply": "生成した返信文", "draft_source": "ai"}
    assert calls == ["丁寧に断る"]
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["draft_reply"] == "生成した返信文" and d["draft_source"] == "ai"
    assert d["draft_generated_at"]


def test_mail_send_flow(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    worker, admin = users
    mid = _add_mail(tmp_path, subject="見積もりの件",
                    references_hdr="<prev@example.com>")
    client.patch("/api/settings", headers=auth(admin),
                 json={"smtp_host": "smtp.example.com"})
    sent = []
    monkeypatch.setattr(
        app_module.mail, "send_reply",
        lambda s, to, subject, body, in_reply_to="", references="":
            sent.append((to, subject, body, in_reply_to, references)))
    # 本文が空なら 400
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "  "}).status_code == 400
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "ご連絡ありがとうございます。"})
    assert r.status_code == 200
    to, subject, body, in_reply_to, references = sent[0]
    assert to == "taro@example.com"
    assert subject == "Re: 見積もりの件"        # 件名は自動で Re: が付く
    assert in_reply_to == "<m1@example.com>"   # スレッドが繋がるヘッダ
    assert references == "<prev@example.com>"
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "replied" and d["replied_at"]
    assert d["reply_subject"] == "Re: 見積もりの件"
    # 返信済みへの再送信は 409
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "x"}).status_code == 409
    # 一般ユーザーは送信できない
    mid2 = _add_mail(tmp_path, n=2)
    assert client.post(f"/api/mail/{mid2}/send", headers=auth(worker),
                       json={"body": "x"}).status_code == 403
    # 監査ログに記録される
    import datetime as _dt
    csv_text = client.get(
        "/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
        headers=auth(admin)).text
    assert "メール返信送信" in csv_text


def test_mail_send_smtp_not_configured(client, users, tmp_path):
    _, admin = users
    mid = _add_mail(tmp_path)
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "本文"}).status_code == 400


def test_mail_send_failure_keeps_status(client, users, tmp_path, monkeypatch):
    from server import app as app_module
    _, admin = users
    mid = _add_mail(tmp_path)
    client.patch("/api/settings", headers=auth(admin),
                 json={"smtp_host": "smtp.example.com"})

    def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module.mail, "send_reply", boom)
    assert client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                       json={"body": "本文"}).status_code == 502
    # 送信失敗時は未対応へ戻り、送信記録も残らない (画面から再試行できる)
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["status"] == "unhandled"
    assert d["reply_body"] == "" and d["replied_at"] is None


def test_mail_fetch_endpoint(client, users, monkeypatch):
    from server import app as app_module
    _, admin = users
    # IMAP 未設定 → 400
    assert client.post("/api/mail/fetch",
                       headers=auth(admin)).status_code == 400
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_host": "imap.example.com"})
    monkeypatch.setattr(app_module.mail, "fetch_new_mail",
                        lambda conn, s: [{"priority": 2}, {"priority": 1}])
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 200 and r.json() == {"fetched": 2}
    # 受信エラーは 502 で原因を返す
    def boom(conn, s):
        raise RuntimeError("login failed")
    monkeypatch.setattr(app_module.mail, "fetch_new_mail", boom)
    assert client.post("/api/mail/fetch",
                       headers=auth(admin)).status_code == 502


def test_check_mail_noop_without_imap(client, users, tmp_path):
    from server import db, mail
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.check_mail(conn) == 0   # IMAP未設定なら何もしない


def test_check_mail_notifies_high_priority(client, users, tmp_path, monkeypatch):
    from server import db, mail
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_host": "imap.example.com"})
    monkeypatch.setattr(
        mail, "fetch_new_mail",
        lambda conn, s: [
            {"priority": 1, "subject": "至急 <!channel> の件", "from_addr": "a@e.com"},
            {"priority": 2, "subject": "普通の件", "from_addr": "b@e.com"}])
    notified = []
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        n = mail.check_mail(conn, notifier=lambda s, text: notified.append(text))
    assert n == 2
    assert len(notified) == 1 and "至急" in notified[0]      # 高のみ通知
    # Slack の特殊トークン (<!channel> 等) は無害化される
    assert "<!channel>" not in notified[0]
    assert "＜!channel＞" in notified[0]


def test_mail_purge(client, users, tmp_path):
    from datetime import datetime, timezone
    from server import db, mail
    _add_mail(tmp_path, n=1, received_at="2020-01-01T00:00:00+00:00")
    mid_new = _add_mail(tmp_path, n=2,
                        received_at="2026-08-20T00:00:00+00:00",
                        fetched_at="2026-08-20T00:00:00+00:00")
    # Date ヘッダ(received_at)が細工されて大昔でも、取り込みが最近なら消えない
    now = datetime.now(timezone.utc).isoformat()
    mid_forged = _add_mail(tmp_path, n=3,
                           received_at="1999-01-01T00:00:00+00:00",
                           fetched_at=now)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 0) == 0     # 0 = 自動削除なし
        assert mail.purge_old_mails(conn, 180) == 1   # 取込が古い1件だけ消える
        rows = conn.execute("SELECT id FROM mails ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [mid_new, mid_forged]


def test_settings_mail_secrets_redacted(client, users, tmp_path):
    _, admin = users
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"imap_host": "imap.example.com",
                           "imap_pass": "imap-secret",
                           "anthropic_api_key": "sk-ant-secret"})
    # PATCH のレスポンスにも秘密情報は含まれない
    body = r.json()
    assert body["imap_pass"] == "" and body["anthropic_api_key"] == ""
    assert body["smtp_pass"] == ""
    s = client.get("/api/settings", headers=auth(admin)).json()
    assert s["imap_pass"] == "" and s["imap_pass_set"] is True
    assert s["anthropic_api_key"] == "" and s["anthropic_api_key_set"] is True
    # 秘密を送らない更新では既存値が保持される
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_folder": "Archive"})
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        stored = db.get_settings(conn)
        assert stored["imap_pass"] == "imap-secret"
        assert stored["anthropic_api_key"] == "sk-ant-secret"
    # 監査ログでは秘密がマスクされる
    import datetime as _dt
    csv_text = client.get(
        "/api/reports/audit.csv?month=" + _dt.datetime.now().strftime("%Y-%m"),
        headers=auth(admin)).text
    assert "imap-secret" not in csv_text and "sk-ant-secret" not in csv_text


def test_imap_port_validation(client, users):
    _, admin = users
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"imap_port": "abc"}).status_code == 400
    assert client.patch("/api/settings", headers=auth(admin),
                        json={"imap_port": "993"}).status_code == 200


def test_test_imap_endpoint(client, users, monkeypatch):
    from server import app as app_module
    worker, admin = users
    # 未設定 → 400
    assert client.post("/api/settings/test-imap",
                       headers=auth(admin)).status_code == 400
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_host": "imap.example.com"})
    monkeypatch.setattr(app_module.mail, "test_imap", lambda s: 42)
    r = client.post("/api/settings/test-imap", headers=auth(admin))
    assert r.status_code == 200 and r.json() == {"ok": True, "count": 42}
    # 接続失敗 → 502 / 一般ユーザー → 403
    def boom(s):
        raise RuntimeError("auth error")
    monkeypatch.setattr(app_module.mail, "test_imap", boom)
    assert client.post("/api/settings/test-imap",
                       headers=auth(admin)).status_code == 502
    assert client.post("/api/settings/test-imap",
                       headers=auth(worker)).status_code == 403


def test_mail_fetch_incremental_uid_and_failure(tmp_path, monkeypatch):
    """差分取得は UID のみで絞り (SINCE は初回だけ)、取得失敗した UID は
    last_uid を進めず次回に再試行する。"""
    from email.message import EmailMessage
    from server import db, mail

    def raw_mail(n):
        msg = EmailMessage()
        msg["From"] = f"s{n}@example.com"
        msg["Subject"] = f"件名{n}"
        msg["Message-ID"] = f"<u{n}@example.com>"
        msg.set_content("本文")
        return msg.as_bytes()

    class FakeIMAP:
        criteria = []

        def __init__(self, mails, fail_uids=()):
            self.mails, self.fail_uids = mails, set(fail_uids)

        def select(self, folder, readonly=False):
            assert readonly  # 受信箱は変更しない
            return ("OK", [str(len(self.mails)).encode()])

        def response(self, key):
            return ("UIDVALIDITY", [b"111"])

        def uid(self, cmd, *args):
            if cmd == "search":
                FakeIMAP.criteria.append(args[1])
                return ("OK",
                        [b" ".join(str(u).encode() for u in sorted(self.mails))])
            u = int(args[0])
            if u in self.fail_uids:
                return ("NO", [None])
            return ("OK", [(b"header", self.mails[u])])

        def logout(self):
            pass

    box = FakeIMAP({101: raw_mail(101), 102: raw_mail(102), 103: raw_mail(103)},
                   fail_uids={102})
    monkeypatch.setattr(mail, "_imap_connect", lambda s: box)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "mail_auto_draft": 0}
        # 1回目: 初回は SINCE で範囲を絞る。UID102 の取得失敗で打ち切り
        added = mail.fetch_new_mail(conn, s)
        assert [a["message_id"] for a in added] == ["<u101@example.com>"]
        assert FakeIMAP.criteria[-1].startswith("(SINCE ")
        # 2回目: 失敗した 102 から再開し、SINCE は付けない (取り逃し防止)
        box.fail_uids = set()
        added = mail.fetch_new_mail(conn, s)
        assert [a["message_id"] for a in added] == [
            "<u102@example.com>", "<u103@example.com>"]
        assert FakeIMAP.criteria[-1] == "(UID 102:*)"
        # 3回目: 新着なし ("UID n:*" が最後の1通を返しても重複しない)
        assert mail.fetch_new_mail(conn, s) == []
        rows = conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        assert rows == 3


def test_mail_send_reply_strips_header_newlines(monkeypatch):
    """デコード済みヘッダに CRLF が含まれても、送信ヘッダは1行に無害化される."""
    import email as email_mod
    from server import mail

    class FakeSMTP:
        last = None

        def __init__(self, host, port, timeout=None):
            self.sent = None
            FakeSMTP.last = self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            self.sent = (frm, to, msg)

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    cfg = {"smtp_host": "smtp.e", "smtp_port": "587", "smtp_user": "u@e",
           "smtp_pass": "", "mail_from": "from@e"}
    mail.send_reply(cfg, "to@e.com", "Re: hello\r\nX-Evil: 1", "本文",
                    in_reply_to="<id\r\n@e.com>", references="")
    frm, to, raw = FakeSMTP.last.sent
    parsed = email_mod.message_from_string(raw)
    assert parsed["Subject"] == "Re: hello X-Evil: 1"  # 改行は空白になり1行
    assert parsed["X-Evil"] is None                    # ヘッダは注入されない
    assert parsed["In-Reply-To"] == "<id @e.com>"
    assert to == ["to@e.com"]
