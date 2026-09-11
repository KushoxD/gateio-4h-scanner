"""Entry point.

RUN_ONCE=1 performs a single scan and exits (suitable for an external cron).
Otherwise the process loops forever, sleeping until the next interval close
plus SCAN_BUFFER_SECONDS so Gate has settled the bar before we read it.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from app.config import Config, load_config
from app.gate import GateClient
from app.health import HealthState, start_health_server
from app.notify import TelegramNotifier
from app.scanner import Scanner, next_close_time
from app.store import AlertStore

log = logging.getLogger("app.main")

_shutdown = threading.Event()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _handle_signal(signum: int, _frame: object) -> None:
    log.info("Received signal %s, shutting down after the current step", signum)
    _shutdown.set()


def seconds_until_next_scan(cfg: Config, now: float | None = None) -> float:
    """Delay until the next interval close plus the settle buffer."""
    now = time.time() if now is None else now
    target = next_close_time(now, cfg.interval_seconds) + cfg.scan_buffer_seconds
    if target <= now:  # we are already inside this bar's buffer window
        target += cfg.interval_seconds
    return target - now


def build_scanner(cfg: Config, store: AlertStore, notifier: TelegramNotifier) -> Scanner:
    client = GateClient(
        base_url=cfg.gate_base,
        timeout=cfg.http_timeout,
        max_retries=cfg.max_retries,
        backoff_base=cfg.backoff_base,
        backoff_cap=cfg.backoff_cap,
        request_interval=cfg.request_interval,
        concurrency=cfg.concurrency,
    )
    return Scanner(cfg, client, store, notifier)


def run_scan(scanner: Scanner, state: HealthState) -> bool:
    try:
        result = scanner.scan()
    except Exception as exc:  # noqa: BLE001 - a bad scan must not kill the loop
        state.record_error(f"{type(exc).__name__}: {exc}")
        log.exception("Scan failed")
        return False
    state.record_scan(result.bar_ts, len(result.hits))
    if result.alerted:
        log.info("Alerted: %s", ", ".join(result.alerted))
    return True


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)

    log.info(
        "Gate.io %s scanner starting | quote=%s min_mcap=%s min_vol=%s "
        "macd=%d/%d/%d ema=%d run_once=%s",
        cfg.interval,
        cfg.quote,
        f"{cfg.min_mcap:,.0f}",
        f"{cfg.min_quote_volume_24h:,.0f}" if cfg.enable_volume_filter else "off",
        cfg.macd_fast,
        cfg.macd_slow,
        cfg.macd_signal,
        cfg.ema_len,
        cfg.run_once,
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = HealthState()

    store = AlertStore(cfg.data_dir)
    store.prune(cfg.dedupe_retention_days)
    notifier = TelegramNotifier(
        cfg.telegram_bot_token,
        cfg.telegram_chat_id,
        dry_run=cfg.dry_run,
        timeout=cfg.http_timeout,
        api_base=cfg.telegram_api_base,
    )
    scanner = build_scanner(cfg, store, notifier)

    # Started after the store exists so the dashboard always has data to read.
    if cfg.enable_health_server:
        start_health_server(cfg.port, state, store=store, cfg=cfg, notifier=notifier)

    try:
        if cfg.run_once:
            ok = run_scan(scanner, state)
            return 0 if ok else 1

        if cfg.scan_on_start:
            # Catch up on the bar that closed before this container booted.
            # Dedupe makes a repeat boot harmless.
            log.info("SCAN_ON_START enabled - scanning the last closed bar now")
            run_scan(scanner, state)

        while not _shutdown.is_set():
            delay = seconds_until_next_scan(cfg)
            state.set_next_scan(time.time() + delay)
            wake_at = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc)
            log.info(
                "Heartbeat | next scan in %.0fs at %s | dedupe rows=%d",
                delay,
                wake_at.isoformat(timespec="seconds"),
                store.count(),
            )
            # Wait in one interruptible block so SIGTERM is honoured promptly.
            if _shutdown.wait(delay):
                break
            run_scan(scanner, state)
            store.prune(cfg.dedupe_retention_days)
    finally:
        notifier.close()
        store.close()

    log.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
