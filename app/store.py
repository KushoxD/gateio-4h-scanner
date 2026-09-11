"""SQLite-backed dedupe store: one alert per (pair, bar timestamp).

``claim`` is the only write path and relies on the primary key plus
``INSERT OR IGNORE``, so the claim is atomic even if two scans overlap.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    pair       TEXT    NOT NULL,
    bar_ts     INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (pair, bar_ts)
);
CREATE INDEX IF NOT EXISTS alerts_created_at_idx ON alerts (created_at);
"""


def resolve_data_dir(preferred: str, fallback: str = "/tmp/gate-scanner") -> str:
    """Return a writable data directory, falling back when the volume is absent.

    Railway only provides ``/data`` when a volume is mounted; without one we
    keep running against ``/tmp`` and accept that dedupe resets on restart.
    """
    for candidate in (preferred, fallback):
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            if candidate != preferred:
                log.warning(
                    "DATA_DIR=%s is not writable; using %s (dedupe resets on restart)",
                    preferred,
                    candidate,
                )
            return candidate
        except OSError as exc:
            log.warning("Data dir %s unusable: %s", candidate, exc)
    raise RuntimeError(f"No writable data directory (tried {preferred!r}, {fallback!r})")


class AlertStore:
    def __init__(self, data_dir: str, filename: str = "scanner.db") -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.path = os.path.join(self.data_dir, filename)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        log.info("Alert store ready at %s", self.path)

    def claim(self, pair: str, bar_ts: int) -> bool:
        """Reserve ``(pair, bar_ts)``. True means this caller should alert."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO alerts (pair, bar_ts, created_at) VALUES (?, ?, ?)",
                (pair, int(bar_ts), int(time.time())),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def release(self, pair: str, bar_ts: int) -> None:
        """Undo a claim, so a failed send can be retried on the next scan."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM alerts WHERE pair = ? AND bar_ts = ?", (pair, int(bar_ts))
            )
            self._conn.commit()

    def was_alerted(self, pair: str, bar_ts: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alerts WHERE pair = ? AND bar_ts = ?", (pair, int(bar_ts))
            ).fetchone()
        return row is not None

    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            cursor = self._conn.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
            self._conn.commit()
        removed = cursor.rowcount or 0
        if removed:
            log.info("Pruned %d dedupe rows older than %d days", removed, retention_days)
        return removed

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "AlertStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
