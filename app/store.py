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

-- Full detail of every hit, so the dashboard can render what was found.
-- The alerts table stays the dedupe ledger; this one is the record.
CREATE TABLE IF NOT EXISTS hits (
    pair             TEXT    NOT NULL,
    base             TEXT,
    bar_ts           INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    timeframe        TEXT,
    close            REAL,
    ema              REAL,
    ema_len          INTEGER,
    macd             REAL,
    signal           REAL,
    market_cap       REAL,
    quote_volume_24h REAL,
    alerted          INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (pair, bar_ts)
);
CREATE INDEX IF NOT EXISTS hits_bar_ts_idx ON hits (bar_ts DESC);
CREATE INDEX IF NOT EXISTS hits_created_at_idx ON hits (created_at);

-- One row per completed scan, for the dashboard's status and funnel.
CREATE TABLE IF NOT EXISTS scans (
    bar_ts             INTEGER PRIMARY KEY,
    finished_at        INTEGER NOT NULL,
    duration           REAL,
    universe_total     INTEGER,
    after_quote        INTEGER,
    after_status       INTEGER,
    after_leveraged    INTEGER,
    after_volume       INTEGER,
    after_mcap         INTEGER,
    scanned            INTEGER,
    hits               INTEGER,
    alerted            INTEGER,
    skipped_duplicate  INTEGER,
    errors             INTEGER
);
CREATE INDEX IF NOT EXISTS scans_finished_at_idx ON scans (finished_at DESC);
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

    # ------------------------------------------------------------- dashboard
    def record_hit(self, hit: object, alerted: bool) -> None:
        """Store the full detail of a hit (idempotent per pair+bar)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO hits (pair, base, bar_ts, interval_seconds, timeframe,
                                  close, ema, ema_len, macd, signal, market_cap,
                                  quote_volume_24h, alerted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair, bar_ts) DO UPDATE SET
                    alerted = MAX(hits.alerted, excluded.alerted)
                """,
                (
                    hit.pair,
                    hit.base,
                    int(hit.bar_ts),
                    int(hit.interval_seconds),
                    hit.timeframe,
                    float(hit.close),
                    float(hit.ema),
                    int(hit.ema_len),
                    float(hit.macd),
                    float(hit.signal),
                    float(hit.market_cap),
                    float(hit.quote_volume_24h),
                    1 if alerted else 0,
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def record_scan(self, result: object) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO scans
                    (bar_ts, finished_at, duration, universe_total, after_quote,
                     after_status, after_leveraged, after_volume, after_mcap,
                     scanned, hits, alerted, skipped_duplicate, errors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(result.bar_ts),
                    int(time.time()),
                    round(float(result.duration), 2),
                    result.universe_total,
                    result.after_quote_filter,
                    result.after_status_filter,
                    result.after_leveraged_filter,
                    result.after_volume_filter,
                    result.after_mcap_filter,
                    result.scanned,
                    len(result.hits),
                    len(result.alerted),
                    result.skipped_duplicate,
                    result.errors,
                ),
            )
            self._conn.commit()

    def recent_hits(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT pair, base, bar_ts, interval_seconds, timeframe, close, ema,
                       ema_len, macd, signal, market_cap, quote_volume_24h, alerted
                FROM hits ORDER BY bar_ts DESC, quote_volume_24h DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        keys = ("pair", "base", "bar_ts", "interval_seconds", "timeframe", "close",
                "ema", "ema_len", "macd", "signal", "market_cap", "quote_volume_24h",
                "alerted")
        return [dict(zip(keys, row)) for row in rows]

    def recent_scans(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT bar_ts, finished_at, duration, universe_total, after_quote,
                       after_status, after_leveraged, after_volume, after_mcap,
                       scanned, hits, alerted, skipped_duplicate, errors
                FROM scans ORDER BY bar_ts DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        keys = ("bar_ts", "finished_at", "duration", "universe_total", "after_quote",
                "after_status", "after_leveraged", "after_volume", "after_mcap",
                "scanned", "hits", "alerted", "skipped_duplicate", "errors")
        return [dict(zip(keys, row)) for row in rows]

    def totals(self) -> dict:
        with self._lock:
            hits = self._conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
            alerted = self._conn.execute(
                "SELECT COUNT(*) FROM hits WHERE alerted = 1"
            ).fetchone()[0]
            scans = self._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        return {"hits": int(hits), "alerted": int(alerted), "scans": int(scans)}

    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            counts = {
                table: self._conn.execute(sql, (cutoff,)).rowcount or 0
                for table, sql in (
                    ("alerts", "DELETE FROM alerts WHERE created_at < ?"),
                    ("hits", "DELETE FROM hits WHERE created_at < ?"),
                    ("scans", "DELETE FROM scans WHERE finished_at < ?"),
                )
            }
            self._conn.commit()

        removed = sum(counts.values())
        if removed:
            log.info(
                "Pruned rows older than %d days: %d dedupe, %d hits, %d scans",
                retention_days,
                counts["alerts"],
                counts["hits"],
                counts["scans"],
            )
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
