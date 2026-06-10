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
    worker, admin = users
    assert client.get(f"/admin?token={worker['token']}").status_code == 403
    r = client.get(f"/admin?token={admin['token']}")
    assert r.status_code == 200
    assert "稼働状況" in r.text


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
    # 2026-05 に2日分、計3時間勤務
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
