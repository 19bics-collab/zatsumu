"""m2m 読み取り係のうち、ブラウザを使わない部分のテスト (playwright 未導入の CI でも動く)."""
import json
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from m2m_sync import core
from m2m_sync.core import Config, StopError

KEY = "k" * 40
ENV = {
    "M2M_EMAIL": "a@example.com", "M2M_PASSWORD": "pw",
    "M2M_INGEST_URL": "https://yadotsugi.example/api/internal/m2m/cleanings", "M2M_INGEST_KEY": KEY,
}


def test_load_config_requires_secrets_and_https():
    cfg = core.load_config(ENV)
    assert cfg.run_at == ("06:00",) and cfg.days_ahead == 60 and cfg.dry_run is False
    for k in ("M2M_EMAIL", "M2M_PASSWORD", "M2M_INGEST_URL", "M2M_INGEST_KEY"):
        with pytest.raises(StopError):
            core.load_config({**ENV, k: ""})
    with pytest.raises(StopError):
        core.load_config({**ENV, "M2M_INGEST_KEY": "short"})
    with pytest.raises(StopError):
        core.load_config({**ENV, "M2M_INGEST_URL": "http://plain.example/x"})
    with pytest.raises(StopError):
        core.load_config({**ENV, "M2M_RUN_AT": "25:00"})
    assert core.load_config({**ENV, "M2M_RUN_AT": "06:00, 13:30", "M2M_DRY_RUN": "true"}).run_at == ("06:00", "13:30")


def test_week_windows_split_inclusive():
    assert core.week_windows(date(2026, 9, 22), date(2026, 10, 6)) == [
        (date(2026, 9, 22), date(2026, 9, 28)),
        (date(2026, 9, 29), date(2026, 10, 5)),
        (date(2026, 10, 6), date(2026, 10, 6)),
    ]


def test_jst_today_and_next_run():
    assert core.jst_today(datetime(2026, 9, 23, 14, 59, tzinfo=timezone.utc)) == date(2026, 9, 23)
    assert core.jst_today(datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)) == date(2026, 9, 24)
    # JST 05:00 → 同日 06:00 / JST 07:00 → 13:00 / JST 14:00 → 翌 06:00
    at = core.next_run(datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc), ("06:00", "13:00"))
    assert at.isoformat() == "2026-09-23T06:00:00+09:00"
    at = core.next_run(datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc), ("06:00", "13:00"))
    assert at.isoformat() == "2026-09-23T13:00:00+09:00"
    at = core.next_run(datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc), ("06:00", "13:00"))
    assert at.isoformat() == "2026-09-24T06:00:00+09:00"


def test_build_payload_trims_fields_dedupes_and_drops_out_of_window():
    rows = [
        {"id": "a", "listingName": "さくらっ家マリン", "cleaningDate": "2026-09-25", "cleanerNames": ["羽切"],
         "note": "メモ", "status": "reported"},
        {"id": "a", "listingName": "さくらっ家マリン", "cleaningDate": "2026-09-25", "status": "cleaning"},
        {"id": "b", "listingName": "x", "cleaningDate": "2026-12-31"},
        {"id": 3, "cleaningDate": "2026-09-25"},
        "junk",
    ]
    p = core.build_payload(rows, date(2026, 9, 22), date(2026, 11, 21))
    assert p["windowStart"] == "2026-09-22" and p["windowEnd"] == "2026-11-21"
    assert p["cleanings"] == [{"id": "a", "listingName": "さくらっ家マリン", "cleaningDate": "2026-09-25",
                               "status": "cleaning"}]
    assert "cleanerNames" not in json.dumps(p), "清掃スタッフ名は外に出さない"


class _Handler(BaseHTTPRequestHandler):
    responses: list = []
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.seen.append({"path": self.path, "key": self.headers.get("X-Internal-Key"), "body": json.loads(body)})
        code, payload = _Handler.responses.pop(0)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    _Handler.responses, _Handler.seen = [], []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/api/internal/m2m/cleanings"
    srv.shutdown()


def _cfg(url, dry=False):
    return Config(email="e", password="p", ingest_url=url, ingest_key=KEY, dry_run=dry)


def test_post_sends_key_and_dryrun_flag(server):
    _Handler.responses = [(200, {"ok": True, "created": 1})]
    res = core.post_payload(_cfg(server, dry=True), {"windowStart": "x", "windowEnd": "y", "cleanings": []},
                            sleep=lambda s: None)
    assert res["created"] == 1
    assert _Handler.seen[0]["key"] == KEY and _Handler.seen[0]["path"].endswith("?dryRun=1")


def test_post_retries_on_5xx_and_409_then_succeeds(server):
    _Handler.responses = [(503, {}), (409, {}), (200, {"ok": True})]
    waits = []
    assert core.post_payload(_cfg(server), {}, sleep=waits.append)["ok"] is True
    assert len(_Handler.seen) == 3 and waits == [30, 60]


def test_post_stops_immediately_on_auth_errors(server):
    for code in (401, 403, 400, 422):
        _Handler.responses = [(code, {"error": "x"})]
        _Handler.seen = []
        with pytest.raises(StopError) as e:
            core.post_payload(_cfg(server), {}, sleep=lambda s: None)
        assert e.value.code == 5 and len(_Handler.seen) == 1


def test_write_status(tmp_path):
    core.write_status(tmp_path, True, {"sent": 3})
    assert (tmp_path / "LAST_STATUS").read_text(encoding="utf-8").startswith("OK ")
