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
    assert lines[0].lstrip("﻿") == "日付,tanaka"
    assert "2026-05-01,3.0" in r.text
    assert "2026-05-02,1.5" in r.text
    assert len([l for l in lines if l.startswith("2026-05")]) == 31  # 全日分


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
                "idx_leave_date", "idx_audit_at", "idx_users_team"):
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
