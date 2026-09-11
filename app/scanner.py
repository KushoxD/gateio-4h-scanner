"""The scan job: Gate.io spot universe -> filters -> indicators -> alerts."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from app.config import Config
from app.gate import Candle, GateClient
from app.indicators import Signal, evaluate
from app.mcap import build_market_cap_map
from app.notify import TelegramNotifier, format_alert
from app.store import AlertStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hit:
    """A pair that satisfied every condition on the last closed bar."""

    pair: str
    base: str
    bar_ts: int
    timeframe: str
    close: float
    ema: float
    ema_len: int
    macd: float
    signal: float
    market_cap: float
    quote_volume_24h: float
    interval_seconds: int

    @property
    def bar_close_ts(self) -> int:
        """Epoch second the bar closed (its open time plus one interval)."""
        return self.bar_ts + self.interval_seconds

    @property
    def bar_close_utc(self) -> str:
        return datetime.fromtimestamp(self.bar_close_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )


@dataclass
class ScanResult:
    bar_ts: int
    scanned: int = 0
    hits: list[Hit] = field(default_factory=list)
    alerted: list[str] = field(default_factory=list)
    skipped_duplicate: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    universe_total: int = 0
    after_quote_filter: int = 0
    after_status_filter: int = 0
    after_leveraged_filter: int = 0
    after_volume_filter: int = 0
    after_mcap_filter: int = 0

    @property
    def duration(self) -> float:
        return time.time() - self.started_at


def current_bar_open(now: float, interval_seconds: int) -> int:
    """Open time of the bar currently forming."""
    return int(now // interval_seconds) * interval_seconds


def last_closed_bar_open(now: float, interval_seconds: int) -> int:
    """Open time of the most recently CLOSED bar."""
    return current_bar_open(now, interval_seconds) - interval_seconds


def next_close_time(now: float, interval_seconds: int) -> int:
    """Epoch second at which the currently forming bar closes."""
    return current_bar_open(now, interval_seconds) + interval_seconds


def is_leveraged(base: str, cfg: Config) -> bool:
    """Leveraged/ETF tokens such as BTC3L, ETH5S, XRPBULL."""
    return bool(cfg.leveraged_pattern.search(base.upper()))


def closed_candles(
    candles: Sequence[Candle], *, now: float, interval_seconds: int
) -> list[Candle]:
    """Drop the still-forming bar (and anything Gate flags as open)."""
    forming_open = current_bar_open(now, interval_seconds)
    return [
        candle
        for candle in candles
        if candle.timestamp < forming_open and candle.window_closed is not False
    ]


class Scanner:
    def __init__(
        self,
        cfg: Config,
        client: GateClient,
        store: AlertStore,
        notifier: TelegramNotifier,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self.notifier = notifier

    # ------------------------------------------------------------- universe
    def build_universe(self, result: ScanResult) -> list[dict[str, Any]]:
        pairs = self.client.currency_pairs()
        result.universe_total = len(pairs)
        cfg = self.cfg

        quoted = [p for p in pairs if str(p.get("quote", "")).upper() == cfg.quote]
        result.after_quote_filter = len(quoted)

        tradable = [
            p for p in quoted if str(p.get("trade_status", "")).lower() == "tradable"
        ]
        result.after_status_filter = len(tradable)

        spot = []
        for pair in tradable:
            base = str(pair.get("base", "")).upper()
            pair_id = str(pair.get("id", ""))
            if not base or not pair_id or base in cfg.exclude_bases:
                continue
            if is_leveraged(base, cfg):
                continue
            spot.append({"id": pair_id, "base": base})
        result.after_leveraged_filter = len(spot)

        if cfg.only_pairs:
            wanted = set(cfg.only_pairs)
            spot = [p for p in spot if p["id"].upper() in wanted]
            log.info("ONLY_PAIRS active: %d pair(s)", len(spot))

        return spot

    def volume_map(self) -> dict[str, float]:
        volumes: dict[str, float] = {}
        for ticker in self.client.tickers():
            pair = str(ticker.get("currency_pair", ""))
            if not pair:
                continue
            try:
                volumes[pair] = float(ticker.get("quote_volume") or 0.0)
            except (TypeError, ValueError):
                volumes[pair] = 0.0
        return volumes

    def market_caps(self) -> dict[str, float]:
        cfg = self.cfg
        gate_currencies: list[dict[str, Any]] = []
        if cfg.mcap_source in ("gate", "auto"):
            try:
                gate_currencies = self.client.currencies()
            except Exception as exc:  # noqa: BLE001 - fall back to CoinGecko
                log.warning("Could not read /spot/currencies: %s", exc)
        return build_market_cap_map(
            cfg.mcap_source,
            gate_currencies,
            coingecko_base=cfg.coingecko_base,
            coingecko_pages=cfg.coingecko_pages,
            coingecko_api_key=cfg.coingecko_api_key,
        )

    # ----------------------------------------------------------------- scan
    def scan(self) -> ScanResult:
        cfg = self.cfg
        now = time.time()
        expected_bar = last_closed_bar_open(now, cfg.interval_seconds)
        result = ScanResult(bar_ts=expected_bar)

        log.info(
            "Scan start | interval=%s | last closed bar %s",
            cfg.interval,
            datetime.fromtimestamp(expected_bar, tz=timezone.utc).isoformat(),
        )

        candidates = self.build_universe(result)

        # Always fetched: the 24h volume is reported in the alert body even when
        # it is not used as a filter.
        volumes = self.volume_map()
        if cfg.enable_volume_filter and cfg.min_quote_volume_24h > 0:
            candidates = [
                p
                for p in candidates
                if volumes.get(p["id"], 0.0) >= cfg.min_quote_volume_24h
            ]
        result.after_volume_filter = len(candidates)

        caps = self.market_caps()
        if cfg.min_mcap > 0 and not caps:
            log.error(
                "No market caps resolved from MCAP_SOURCE=%s - every pair will be "
                "filtered out. Check MCAP_SOURCE / CoinGecko reachability.",
                cfg.mcap_source,
            )
        if cfg.min_mcap > 0:
            kept = []
            for pair in candidates:
                cap = caps.get(pair["base"], 0.0)
                if cap >= cfg.min_mcap:
                    kept.append(pair)
            candidates = kept
        result.after_mcap_filter = len(candidates)

        if cfg.max_pairs > 0:
            candidates = candidates[: cfg.max_pairs]

        log.info(
            "Universe: %d pairs -> %s quote %d -> tradable %d -> non-leveraged %d "
            "-> volume %d -> mcap %d (scanning %d)",
            result.universe_total,
            cfg.quote,
            result.after_quote_filter,
            result.after_status_filter,
            result.after_leveraged_filter,
            result.after_volume_filter,
            result.after_mcap_filter,
            len(candidates),
        )

        outcomes = self.client.map(self._fetch_candles, [p["id"] for p in candidates])
        base_by_pair = {p["id"]: p["base"] for p in candidates}

        for pair_id, candles, error in outcomes:
            if error is not None:
                result.errors += 1
                log.warning("Candles for %s failed: %s", pair_id, error)
                continue
            result.scanned += 1
            hit = self._evaluate_pair(
                pair_id,
                base_by_pair.get(pair_id, ""),
                candles or [],
                expected_bar=expected_bar,
                now=now,
                market_cap=caps.get(base_by_pair.get(pair_id, ""), 0.0),
                quote_volume=volumes.get(pair_id, 0.0),
            )
            if hit is not None:
                result.hits.append(hit)

        self._dispatch(result)
        return result

    def _fetch_candles(self, pair_id: str) -> list[Candle]:
        return self.client.candlesticks(
            pair_id, interval=self.cfg.interval, limit=self.cfg.candle_limit
        )

    def _evaluate_pair(
        self,
        pair_id: str,
        base: str,
        candles: Sequence[Candle],
        *,
        expected_bar: int,
        now: float,
        market_cap: float,
        quote_volume: float,
    ) -> Hit | None:
        cfg = self.cfg
        closed = closed_candles(candles, now=now, interval_seconds=cfg.interval_seconds)
        if len(closed) < cfg.min_candles:
            log.debug("%s: only %d closed bars, need %d", pair_id, len(closed), cfg.min_candles)
            return None

        newest = closed[-1]
        if cfg.strict_bar_ts and newest.timestamp != expected_bar:
            log.debug(
                "%s: newest closed bar %s != expected %s, skipping",
                pair_id,
                newest.timestamp,
                expected_bar,
            )
            return None

        signal: Signal | None = evaluate(
            [candle.close for candle in closed],
            fast=cfg.macd_fast,
            slow=cfg.macd_slow,
            signal=cfg.macd_signal,
            ema_len=cfg.ema_len,
        )
        if signal is None or not signal.triggered:
            return None

        return Hit(
            pair=pair_id,
            base=base,
            bar_ts=newest.timestamp,
            timeframe=cfg.timeframe_label,
            close=signal.close,
            ema=signal.ema,
            ema_len=cfg.ema_len,
            macd=signal.macd,
            signal=signal.signal,
            market_cap=market_cap,
            quote_volume_24h=quote_volume,
            interval_seconds=cfg.interval_seconds,
        )

    # ----------------------------------------------------------------- alert
    def _dispatch(self, result: ScanResult) -> None:
        cfg = self.cfg
        # Strongest signals first, so a MAX_ALERTS_PER_SCAN cap keeps the best.
        ordered = sorted(result.hits, key=lambda h: h.quote_volume_24h, reverse=True)

        sent = 0
        for hit in ordered:
            # Check the cap BEFORE claiming: a claim we do not send would burn
            # this pair's only chance to alert on this bar.
            if cfg.max_alerts_per_scan > 0 and sent >= cfg.max_alerts_per_scan:
                log.warning(
                    "MAX_ALERTS_PER_SCAN=%d reached; %s suppressed",
                    cfg.max_alerts_per_scan,
                    hit.pair,
                )
                continue
            if not self.store.claim(hit.pair, hit.bar_ts):
                result.skipped_duplicate += 1
                log.info("Skipping %s @ %s - already alerted", hit.pair, hit.bar_ts)
                continue
            if self.notifier.send(format_alert(hit)):
                result.alerted.append(hit.pair)
                self.store.record_hit(hit, alerted=True)
                sent += 1
            else:
                # Release the claim so the next scan can retry this alert.
                self.store.release(hit.pair, hit.bar_ts)
                result.errors += 1
                log.error("Alert delivery failed for %s; claim released", hit.pair)

        # Every hit is recorded, alerted or not, so the dashboard shows the
        # full picture (duplicates, capped, and failed sends included).
        alerted_pairs = set(result.alerted)
        for hit in ordered:
            if hit.pair not in alerted_pairs:
                self.store.record_hit(hit, alerted=False)
        self.store.record_scan(result)

        log.info(
            "Scan done in %.1fs | scanned=%d hits=%d alerted=%d dupes=%d errors=%d",
            result.duration,
            result.scanned,
            len(result.hits),
            len(result.alerted),
            result.skipped_duplicate,
            result.errors,
        )
