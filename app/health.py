"""Optional tiny HTTP health endpoint.

Railway worker services need no port; this exists only for plans/setups that
insist on one. Scanning always runs on the main thread - this server is a
daemon thread that answers /health and /.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


class HealthState:
    """Mutable snapshot the HTTP handler reads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.last_scan_at: float | None = None
        self.last_scan_bar_ts: int | None = None
        self.last_scan_hits: int = 0
        self.last_error: str | None = None
        self.scans_completed: int = 0

    def record_scan(self, bar_ts: int, hits: int) -> None:
        with self.lock:
            self.last_scan_at = time.time()
            self.last_scan_bar_ts = bar_ts
            self.last_scan_hits = hits
            self.last_error = None
            self.scans_completed += 1

    def record_error(self, message: str) -> None:
        with self.lock:
            self.last_error = message

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "status": "ok",
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "scans_completed": self.scans_completed,
                "last_scan_at": self.last_scan_at,
                "last_scan_bar_ts": self.last_scan_bar_ts,
                "last_scan_hits": self.last_scan_hits,
                "last_error": self.last_error,
            }


def start_health_server(port: int, state: HealthState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/health", "/healthz"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return  # keep request noise out of the scanner log

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    log.info("Health server listening on 0.0.0.0:%d/health", port)
    return server
