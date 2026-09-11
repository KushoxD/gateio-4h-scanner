"""Full pipeline against a local stand-in for Gate.io and Telegram.

The sandbox this was developed in cannot reach api.gateio.ws, so the wire
format is reproduced here from Gate's v4 spot documentation.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import Config
from app.indicators import evaluate
from app.gate import GateClient
from app.notify import TelegramNotifier, format_alert
from app.scanner import Scanner, last_closed_bar_open
from app.store import AlertStore

FOUR_HOURS = 14400


def trigger_closes():
    """Dip-then-rally series whose FINAL bar is the golden cross above zero.

    Built once and trimmed to the exact bar where all three conditions first
    hold, so the scanner has to read the last closed bar to find it.
    """
    up = [100 + 3.0 * i for i in range(40)]
    dip = [up[-1] - 2.0 * i for i in range(1, 13)]
    rally = [dip[-1] + 6.0 * i for i in range(1, 9)]
    series = up + dip + rally
    for cut in range(45, len(series) + 1):
        signal = evaluate(series[:cut])
        if signal is not None and signal.triggered:
            return series[:cut]
    raise AssertionError("fixture series never triggers")


TRIGGER_CLOSES = trigger_closes()


def flat_closes():
    return [50.0] * len(TRIGGER_CLOSES)


class FakeGate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    telegram_messages = []
    rate_limit_once = {"candles": True}

    def _json(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/v4/spot/currency_pairs":
            return self._json(
                [
                    {"id": "WIN_USDT", "base": "WIN", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "WIN2_USDT", "base": "WIN2", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "FLAT_USDT", "base": "FLAT", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "SMALL_USDT", "base": "SMALL", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "THIN_USDT", "base": "THIN", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "HALT_USDT", "base": "HALT", "quote": "USDT", "trade_status": "untradable"},
                    {"id": "WIN3L_USDT", "base": "WIN3L", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "WIN5S_USDT", "base": "WIN5S", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "WIN_BTC", "base": "WIN", "quote": "BTC", "trade_status": "tradable"},
                ]
            )

        if path == "/api/v4/spot/currencies":
            return self._json(
                [
                    {"currency": "WIN", "delisted": False, "market_cap": "250000000"},
                    {"currency": "WIN2", "delisted": False, "market_cap": "150000000"},
                    {"currency": "FLAT", "delisted": False, "market_cap": "900000000"},
                    {"currency": "SMALL", "delisted": False, "market_cap": "1000000"},
                    {"currency": "THIN", "delisted": False, "market_cap": "500000000"},
                    {"currency": "HALT", "delisted": False, "market_cap": "500000000"},
                ]
            )

        if path == "/api/v4/spot/tickers":
            return self._json(
                [
                    {"currency_pair": "WIN_USDT", "last": "1.0", "quote_volume": "5000000"},
                    {"currency_pair": "WIN2_USDT", "last": "1.0", "quote_volume": "2000000"},
                    {"currency_pair": "FLAT_USDT", "last": "50.0", "quote_volume": "4000000"},
                    {"currency_pair": "SMALL_USDT", "last": "1.0", "quote_volume": "3000000"},
                    {"currency_pair": "THIN_USDT", "last": "1.0", "quote_volume": "1000"},
                ]
            )

        if path == "/api/v4/spot/candlesticks":
            # Exercise the 429 backoff path exactly once.
            if FakeGate.rate_limit_once.get("candles"):
                FakeGate.rate_limit_once["candles"] = False
                return self._json({"label": "TOO_MANY_REQUESTS"}, status=429,
                                  headers={"Retry-After": "0"})

            pair = query.get("currency_pair", [""])[0]
            assert int(query.get("limit", ["0"])[0]) >= len(TRIGGER_CLOSES)
            closes = flat_closes() if pair == "FLAT_USDT" else list(TRIGGER_CLOSES)

            last_closed = last_closed_bar_open(time.time(), FOUR_HOURS)
            rows = []
            for offset, close in enumerate(reversed(closes)):
                ts = last_closed - offset * FOUR_HOURS
                rows.append([str(ts), "123456.0", f"{close:.8f}", f"{close * 1.01:.8f}",
                             f"{close * 0.99:.8f}", f"{close:.8f}", "1000.0", "true"])
            rows.reverse()
            # Gate also returns the still-forming bar; the scanner must drop it.
            forming = last_closed + FOUR_HOURS
            rows.append([str(forming), "1.0", "999999.0", "999999.0", "1.0", "1.0", "1.0", "false"])
            return self._json(rows)

        return self._json({"label": "NOT_FOUND"}, status=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeGate.telegram_messages.append(body)
        return self._json({"ok": True, "result": {"message_id": len(FakeGate.telegram_messages)}})

    def log_message(self, *_args):
        return


@pytest.fixture()
def server():
    FakeGate.telegram_messages = []
    FakeGate.rate_limit_once = {"candles": True}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGate)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def cfg(monkeypatch, server, tmp_path):
    env = {
        "GATE_BASE": f"{server}/api/v4",
        "MCAP_SOURCE": "gate",
        "MIN_MCAP": "10000000",
        "MIN_QUOTE_VOLUME_24H": "100000",
        "DATA_DIR": str(tmp_path),
        "BACKOFF_BASE": "0.01",
        "REQUEST_INTERVAL": "0",
        "CONCURRENCY": "4",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "-100123",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Config()


def _scanner(cfg, server, tmp_path):
    client = GateClient(
        base_url=cfg.gate_base,
        request_interval=0,
        concurrency=cfg.concurrency,
        backoff_base=0.01,
        backoff_cap=0.05,
    )
    store = AlertStore(str(tmp_path))
    notifier = TelegramNotifier(
        cfg.telegram_bot_token, cfg.telegram_chat_id, api_base=server, max_retries=1
    )
    return Scanner(cfg, client, store, notifier), store


def test_full_scan_alerts_only_the_qualifying_pair(cfg, server, tmp_path):
    scanner, store = _scanner(cfg, server, tmp_path)
    try:
        result = scanner.scan()
    finally:
        store.close()

    # WIN_BTC (wrong quote), HALT (untradable), WIN3L/WIN5S (leveraged),
    # SMALL (mcap), THIN (volume) are all filtered before any candle fetch.
    assert result.after_quote_filter == 8
    assert result.after_status_filter == 7
    assert result.after_leveraged_filter == 5
    assert result.after_volume_filter == 4
    assert result.after_mcap_filter == 3  # WIN + WIN2 + FLAT

    # Both winners fire; the more liquid one is sent first.
    assert sorted(hit.pair for hit in result.hits) == ["WIN2_USDT", "WIN_USDT"]
    assert result.alerted == ["WIN_USDT", "WIN2_USDT"]
    assert result.errors == 0

    assert len(FakeGate.telegram_messages) == 2
    text = FakeGate.telegram_messages[0]["text"]
    for fragment in ("WIN_USDT", "Gate", "4H", "Close:", "EMA20:", "MACD:", "Signal:",
                     "Market cap:", "24h quote vol:", "https://www.gate.io/trade/WIN_USDT"):
        assert fragment in text


def test_the_forming_candle_is_never_used(cfg, server, tmp_path):
    scanner, store = _scanner(cfg, server, tmp_path)
    try:
        result = scanner.scan()
    finally:
        store.close()
    hit = next(h for h in result.hits if h.pair == "WIN_USDT")
    # The fake forming bar closes at 999999.0; using it would show up here.
    assert hit.close < 1000
    assert hit.bar_ts == last_closed_bar_open(time.time(), FOUR_HOURS)
    assert hit.bar_ts % FOUR_HOURS == 0


def test_rerunning_the_same_bar_does_not_alert_twice(cfg, server, tmp_path):
    scanner, store = _scanner(cfg, server, tmp_path)
    try:
        first = scanner.scan()
        second = scanner.scan()
    finally:
        store.close()

    assert first.alerted == ["WIN_USDT", "WIN2_USDT"]
    assert second.alerted == []
    assert second.skipped_duplicate == 2
    assert len(second.hits) == 2  # still detected, just not re-sent
    assert len(FakeGate.telegram_messages) == 2


def test_alert_body_reports_mcap_and_volume_from_the_api(cfg, server, tmp_path):
    scanner, store = _scanner(cfg, server, tmp_path)
    try:
        result = scanner.scan()
    finally:
        store.close()
    hit = next(h for h in result.hits if h.pair == "WIN_USDT")
    assert hit.market_cap == 250_000_000
    assert hit.quote_volume_24h == 5_000_000
    body = format_alert(hit)
    assert "$250.00M" in body
    assert "$5.00M" in body


def test_conditions_hold_on_the_reported_hit(cfg, server, tmp_path):
    scanner, store = _scanner(cfg, server, tmp_path)
    try:
        result = scanner.scan()
    finally:
        store.close()
    hit = next(h for h in result.hits if h.pair == "WIN_USDT")
    assert hit.macd > hit.signal
    assert hit.macd > 0
    assert hit.close > hit.ema


def test_alert_cap_suppresses_without_burning_the_dedupe_claim(cfg, server, tmp_path, monkeypatch):
    """A capped alert must stay sendable on the next scan.

    Claiming before checking the cap would mark the pair as alerted for this
    bar and silently drop it forever.
    """
    monkeypatch.setenv("MAX_ALERTS_PER_SCAN", "1")
    capped = Config()

    scanner, store = _scanner(capped, server, tmp_path)
    try:
        first = scanner.scan()
        assert first.alerted == ["WIN_USDT"]  # highest 24h volume wins the slot
        assert len(first.hits) == 2
        assert store.was_alerted("WIN2_USDT", first.bar_ts) is False

        # Raising the cap lets the suppressed pair through on the next scan.
        monkeypatch.setenv("MAX_ALERTS_PER_SCAN", "40")
        scanner.cfg = Config()
        second = scanner.scan()
    finally:
        store.close()

    assert second.alerted == ["WIN2_USDT"]
    assert second.skipped_duplicate == 1
    assert len(FakeGate.telegram_messages) == 2
