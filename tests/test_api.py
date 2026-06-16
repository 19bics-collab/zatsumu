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
    assert all("token" not in u for u in lst)

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


def test_to_jpeg_widens_cap_for_multimonitor():
    import io
    from PIL import Image
    from client import capture
    # マルチモニターを並べた横長画像(aspect>2)は上限幅が広がり縮みすぎない
    wide = Image.new("RGB", (3840, 1080), "white")
    out = Image.open(io.BytesIO(capture.to_jpeg(wide, max_width=1280)))
    assert out.width == 3840
    # 単一モニター相当(16:9)は従来どおり max_width に縮小
    normal = Image.new("RGB", (1920, 1080), "white")
    out2 = Image.open(io.BytesIO(capture.to_jpeg(normal, max_width=1280)))
    assert out2.width == 1280


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
    conn.close()


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
