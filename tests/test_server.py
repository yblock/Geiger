import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from geiger import demo, settings
from geiger.logger import Hub, backup_path
from geiger.server import DashboardServer, pick_step
from geiger.store import Store


class FakeLogger:
    def __init__(self):
        self.sync_requests = 0
        self.jobs = []

    def status(self):
        return {"state": "live"}

    def request_sync(self):
        self.sync_requests += 1

    def submit(self, job, argument=None, timeout=30):
        self.jobs.append((job, argument))
        if argument and "save_mode" in argument and argument["save_mode"] > 5:
            raise ValueError("Save to memory: not one of the options")
        return demo.SETTINGS

    def settings_view(self):
        return {"connected": True, "read_at": 0, "fields": settings.describe_fields(demo.SETTINGS)}


@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / "test.db")
    now = int(time.time())
    for i in range(300):
        store.add_live(now - 300 + i, 1 if i % 3 == 0 else 0)
    store.set_meta("calibration", {"points": [[1538, 10.0], [15380, 100.0], [30760, 200.0]]})
    logger = FakeLogger()
    srv = DashboardServer(("127.0.0.1", 0), store, logger, Hub())
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.now = now
    yield srv
    srv.shutdown()
    srv.server_close()


def get(srv, path, **headers):
    request = urllib.request.Request(srv.base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as r:
        return r.status, r.headers, r.read()


def status_of(srv, path, method="GET", **headers):
    request = urllib.request.Request(srv.base + path, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_pick_step():
    assert pick_step(600, 600) == 1
    assert pick_step(3600, 600) == 10
    assert pick_step(86400 * 365 * 5, 600) == 604800


def test_series_and_summary(server):
    _, _, body = get(server, f"/api/series?start={server.now - 300}&end={server.now}&points=300")
    data = json.loads(body)
    assert data["step"] == 1
    full = [p for p in data["points"] if p[0] >= server.now - 240]  # whole minute of data behind it
    assert full and all(p[1] == pytest.approx(20) for p in full)
    assert data["summary"]["counts"] == 100
    assert data["summary"]["dose_usv"] > 0


def test_fit_narrows_the_chart_to_the_readings(server):
    month = f"/api/series?start={server.now - 30 * 86400}&end={server.now}&points=600"
    wide = json.loads(get(server, month)[2])
    fitted = json.loads(get(server, month + "&fit=1")[2])
    assert wide["start"] == server.now - 30 * 86400
    # five minutes of data: the chart spans just those, at per-second resolution
    assert fitted["start"] == server.now - 300
    assert fitted["end"] == server.now
    assert fitted["step"] < wide["step"]
    assert fitted["range_start"] == server.now - 30 * 86400
    # the stats still describe the whole selected range
    assert fitted["summary"] == wide["summary"]


def test_fit_keeps_an_empty_range_as_asked(server):
    path = f"/api/series?start={server.now - 7200}&end={server.now - 3600}&fit=1"
    data = json.loads(get(server, path)[2])
    assert (data["start"], data["end"], data["points"]) == (server.now - 7200, server.now - 3600, [])


def test_recent_and_export(server):
    _, _, body = get(server, "/api/recent?seconds=60")
    assert 55 <= len(json.loads(body)) <= 61
    _, headers, body = get(server, f"/api/export.csv?start={server.now - 300}&end={server.now}")
    assert headers["Content-Type"].startswith("text/csv")
    lines = body.decode().splitlines()
    assert lines[0].startswith("minute_utc,")
    assert len(lines) >= 5


def test_bad_requests(server):
    assert status_of(server, "/api/series") == 400  # no start
    assert status_of(server, "/api/series?start=10&end=5") == 400
    assert status_of(server, "/static/..%2Fstore.py") == 404
    assert status_of(server, "/nope") == 404


def test_sync_needs_the_custom_header(server):
    assert status_of(server, "/api/sync", method="POST") == 403
    assert status_of(server, "/api/sync", method="POST", **{"X-Geiger": "1"}) == 202
    assert server.logger.sync_requests == 1


def test_dashboard_page(server):
    status, headers, body = get(server, "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"/static/app.js" in body


def post_json(srv, path, body):
    request = urllib.request.Request(
        srv.base + path, method="POST", data=json.dumps(body).encode(),
        headers={"X-Geiger": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_settings_read_and_write(server):
    _, _, body = get(server, "/api/settings")
    keys = {f["key"] for f in json.loads(body)["fields"]}
    assert {"speaker", "save_mode", "cal_usv_0"} <= keys
    status, data = post_json(server, "/api/settings", {"changes": {"speaker": 1}})
    assert status == 200 and data["ok"]
    assert server.logger.jobs[-1] == ("write", {"speaker": 1})
    status, data = post_json(server, "/api/settings", {"changes": {"save_mode": 9}})
    assert status == 400 and "not one of" in data["error"]
    assert post_json(server, "/api/settings", {"changes": {}})[0] == 400
    assert post_json(server, "/api/settings/refresh", {})[0] == 200
    assert server.logger.jobs[-1] == ("read", None)


def test_settings_writes_need_the_custom_header(server):
    assert status_of(server, "/api/settings", method="POST") == 403


def test_counter_memory_downloads(server):
    assert status_of(server, "/api/counter-memory.bin") == 404  # nothing synced yet
    stamp = bytes([26, 10, 23, 7, 0, 0])
    image = b"\x55\xaa\x00" + stamp + b"\x55\xaa\x01" + bytes([1, 0, 2])
    backup_path(server.store).write_bytes(image)
    _, headers, body = get(server, "/api/counter-memory.bin")
    assert body == image and "attachment" in headers["Content-Disposition"]
    _, _, body = get(server, "/api/counter-memory.csv")
    lines = body.decode().splitlines()
    assert lines[0].startswith("start_utc,") and len(lines) == 4
