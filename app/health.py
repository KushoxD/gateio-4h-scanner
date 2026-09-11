"""HTTP server: the alert dashboard, a JSON state feed, and a health probe.

Scanning always runs on the main thread; this is a daemon thread that only
reads. Routes:
    GET /            dashboard (HTML)
    GET /api/state   everything the dashboard renders (JSON)
    GET /health      liveness probe (JSON)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.dashboard import PAGE

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
        self.next_scan_at: float | None = None

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

    def set_next_scan(self, when: float) -> None:
        with self.lock:
            self.next_scan_at = when

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": "ok",
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "scans_completed": self.scans_completed,
                "last_scan_at": self.last_scan_at,
                "last_scan_bar_ts": self.last_scan_bar_ts,
                "last_scan_hits": self.last_scan_hits,
                "next_scan_at": self.next_scan_at,
                "last_error": self.last_error,
            }


def build_state(state: HealthState, store: Any, cfg: Any, notifier: Any) -> dict[str, Any]:
    """Assemble the full payload the dashboard renders."""
    payload = state.snapshot()
    scans = store.recent_scans(limit=20)
    last = scans[0] if scans else {}
    interval_seconds = getattr(cfg, "interval_seconds", 14400)

    payload.update(
        {
            "telegram_enabled": bool(getattr(notifier, "enabled", False)),
            "hits": store.recent_hits(limit=60),
            "scans": scans,
            "last_scan": {**last, "interval_seconds": interval_seconds},
            "last_bar_close_ts": (
                last["bar_ts"] + interval_seconds if last.get("bar_ts") else None
            ),
            "totals": store.totals(),
            "config": {
                "interval": cfg.interval,
                "quote": cfg.quote,
                "macd_fast": cfg.macd_fast,
                "macd_slow": cfg.macd_slow,
                "macd_signal": cfg.macd_signal,
                "ema_len": cfg.ema_len,
                "min_mcap": cfg.min_mcap,
                "min_quote_volume_24h": (
                    cfg.min_quote_volume_24h if cfg.enable_volume_filter else 0
                ),
            },
        }
    )
    return payload


def start_health_server(
    port: int,
    state: HealthState,
    store: Any = None,
    cfg: Any = None,
    notifier: Any = None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0].rstrip("/") or "/"

            if path == "/" and store is not None:
                return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

            if path == "/api/state":
                if store is None:
                    return self._send(
                        json.dumps(state.snapshot()).encode(), "application/json"
                    )
                try:
                    payload = build_state(state, store, cfg, notifier)
                except Exception as exc:  # noqa: BLE001 - never 500 the dashboard
                    log.warning("Could not build dashboard state: %s", exc)
                    payload = {**state.snapshot(), "error": str(exc)}
                return self._send(json.dumps(payload).encode(), "application/json")

            if path in ("/", "/health", "/healthz"):
                return self._send(
                    json.dumps(state.snapshot()).encode(), "application/json"
                )

            self._send(b'{"error":"not found"}', "application/json", status=404)

        def log_message(self, *_args: object) -> None:
            return  # keep request noise out of the scanner log

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    log.info("Dashboard listening on 0.0.0.0:%d (/ and /health)", port)
    return server
