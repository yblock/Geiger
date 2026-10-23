"""Dashboard HTTP server: static page, JSON API, and a server-sent event stream."""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .logger import Hub, Logger
from .store import Store

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# Chart resolutions, seconds. Below a minute each point is the rolling 60 s CPM.
STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
         43200, 86400, 172800, 604800)
MAX_POINTS = 2000


def pick_step(span: int, points: int) -> int:
    return next((s for s in STEPS if span / s <= points), STEPS[-1])


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: Store, logger: Logger, hub: Hub):
        super().__init__(address, Handler)
        self.store = store
        self.logger = logger
        self.hub = hub


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "geiger"

    def log_message(self, format, *args):  # route access logs through logging, quietly
        log.debug("%s %s", self.address_string(), format % args)

    # routing

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        routes = {
            "/": lambda: self._static("index.html"),
            "/api/status": lambda: self._json(self.server.logger.status()),
            "/api/series": lambda: self._series(query),
            "/api/recent": lambda: self._recent(query),
            "/api/events": self._events,
            "/api/export.csv": lambda: self._export(query),
        }
        try:
            if url.path in routes:
                routes[url.path]()
            elif url.path.startswith("/static/"):
                self._static(url.path.removeprefix("/static/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("error serving %s", self.path)
            self._json({"error": "internal error; see the logger's log"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    def finish(self):
        super().finish()
        self.server.store.release()  # each request runs on its own short-lived thread

    def do_POST(self):
        # A custom header can't be sent cross-origin without a CORS preflight, which
        # this server never approves, so other web pages can't trigger actions.
        if self.headers.get("X-Geiger") != "1":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if urlparse(self.path).path == "/api/sync":
            self.server.logger.request_sync()
            self._json({"ok": True}, HTTPStatus.ACCEPTED)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # responses

    def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=HTTPStatus.OK):
        self._send(json.dumps(data).encode(), "application/json", status)

    def _static(self, name: str):
        path = STATIC / name
        if not re.fullmatch(r"[\w.-]+", name) or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send(path.read_bytes(), CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def _range(self, query: dict) -> tuple[int, int]:
        end = int(query.get("end") or time.time())
        start = int(query["start"])
        if not 0 <= start < end:
            raise ValueError("start must be before end")
        return start, end

    def _series(self, query: dict):
        start, end = self._range(query)
        points = min(int(query.get("points", 600)), MAX_POINTS)
        tz = int(query.get("tz", 0))
        step = pick_step(end - start, points)
        store = self.server.store
        bounds = store.bounds()
        # an "all time" request starts at the epoch; measure coverage from the first data
        covered_from = max(start, bounds[0]) if bounds else start
        self._json(
            {
                "start": start,
                "end": end,
                "step": step,
                "points": store.rate_series(start, end, step, tz),
                "summary": store.summary(covered_from, end, store.calibration()),
            }
        )

    def _recent(self, query: dict):
        seconds = min(int(query.get("seconds", 120)), 3600)
        rows = self.server.store.recent(int(time.time()) - seconds)
        self._json([[ts, count] for ts, s, count in rows if s == 1])

    def _export(self, query: dict):
        start, end = self._range(query)
        calibration = self.server.store.calibration()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["minute_utc", "minute_local", "counts", "seconds", "cpm", "usv_h"])
        for ts, count, seconds in self.server.store.minutes_between(start, end):
            cpm = 60 * count / seconds if seconds else None
            moment = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            writer.writerow([
                moment.isoformat(),
                moment.astimezone().isoformat(),
                count,
                seconds,
                f"{cpm:.2f}" if cpm is not None else "",
                f"{calibration.usv_h(cpm):.4f}" if cpm is not None and calibration else "",
            ])
        name = f"geiger-{datetime.date.fromtimestamp(start)}-{datetime.date.fromtimestamp(end)}.csv"
        self._send(
            out.getvalue().encode(),
            "text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    def _events(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        hub = self.server.hub
        seq = hub.seq
        self._event("status", self.server.logger.status())
        while True:
            events = hub.wait(seq, timeout=15)
            if not events:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                continue
            for seq, kind, data in events:
                self._event(kind, data)

    def _event(self, kind: str, data: dict):
        self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()
