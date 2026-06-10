import os

import pytest
from fastapi.testclient import TestClient


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
