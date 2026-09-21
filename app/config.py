"""Environment-driven configuration for the Gate.io 4H scanner.

Every knob the scanner uses is read here exactly once, at import time, so the
rest of the code never touches os.environ directly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    raw = _str(name, str(default))
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name, "1" if default else "0").lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _csv(name: str, default: str = "") -> list[str]:
    raw = _str(name, default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


# Interval label -> seconds. Gate.io accepts these labels verbatim.
INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "8h": 28800,
    "1d": 86400,
}


@dataclass(frozen=True)
class Config:
    # --- market selection -------------------------------------------------
    quote: str = field(default_factory=lambda: _str("QUOTE", "USDT").upper())
    interval: str = field(default_factory=lambda: _str("INTERVAL", "4h").lower())
    min_mcap: float = field(default_factory=lambda: _float("MIN_MCAP", 10_000_000))
    min_quote_volume_24h: float = field(
        default_factory=lambda: _float("MIN_QUOTE_VOLUME_24H", 100_000)
    )
    enable_volume_filter: bool = field(
        default_factory=lambda: _bool("ENABLE_VOLUME_FILTER", True)
    )
    leveraged_regex: str = field(
        # Gate names its leveraged tokens BTC3L / ETH5S / .... Deliberately NOT
        # matching BULL/BEAR/UP/DOWN by default: those suffixes also occur inside
        # legitimate tickers such as JUP and SYRUP.
        default_factory=lambda: _str("LEVERAGED_REGEX", r".+(?:3|4|5)(?:L|S)$")
    )
    exclude_bases: list[str] = field(default_factory=lambda: _csv("EXCLUDE_BASES"))
    only_pairs: list[str] = field(default_factory=lambda: _csv("ONLY_PAIRS"))
    max_pairs: int = field(default_factory=lambda: _int("MAX_PAIRS", 0))

    # --- market cap -------------------------------------------------------
    # gate | coingecko | auto (Gate first, CoinGecko fills the gaps)
    mcap_source: str = field(default_factory=lambda: _str("MCAP_SOURCE", "auto").lower())
    coingecko_pages: int = field(default_factory=lambda: _int("COINGECKO_PAGES", 8))
    coingecko_api_key: str = field(default_factory=lambda: _str("COINGECKO_API_KEY", ""))
    coingecko_base: str = field(
        default_factory=lambda: _str("COINGECKO_BASE", "https://api.coingecko.com/api/v3")
    )

    # --- indicators -------------------------------------------------------
    macd_fast: int = field(default_factory=lambda: _int("MACD_FAST", 12))
    macd_slow: int = field(default_factory=lambda: _int("MACD_SLOW", 26))
    macd_signal: int = field(default_factory=lambda: _int("MACD_SIGNAL", 9))
    ema_len: int = field(default_factory=lambda: _int("EMA_LEN", 20))
    candle_limit: int = field(default_factory=lambda: _int("CANDLE_LIMIT", 80))

    # --- http / rate limits ----------------------------------------------
    gate_base: str = field(
        default_factory=lambda: _str("GATE_BASE", "https://api.gateio.ws/api/v4")
    )
    concurrency: int = field(default_factory=lambda: _int("CONCURRENCY", 8))
    request_interval: float = field(default_factory=lambda: _float("REQUEST_INTERVAL", 0.06))
    http_timeout: float = field(default_factory=lambda: _float("HTTP_TIMEOUT", 20.0))
    max_retries: int = field(default_factory=lambda: _int("MAX_RETRIES", 5))
    backoff_base: float = field(default_factory=lambda: _float("BACKOFF_BASE", 1.0))
    backoff_cap: float = field(default_factory=lambda: _float("BACKOFF_CAP", 30.0))

    # --- alerts -----------------------------------------------------------
    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))
    telegram_api_base: str = field(
        default_factory=lambda: _str("TELEGRAM_API_BASE", "https://api.telegram.org")
    )
    max_alerts_per_scan: int = field(default_factory=lambda: _int("MAX_ALERTS_PER_SCAN", 40))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))

    # --- storage / runtime -----------------------------------------------
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "/data"))
    dedupe_retention_days: int = field(
        default_factory=lambda: _int("DEDUPE_RETENTION_DAYS", 30)
    )
    run_once: bool = field(default_factory=lambda: _bool("RUN_ONCE", False))
    scan_on_start: bool = field(default_factory=lambda: _bool("SCAN_ON_START", True))
    scan_buffer_seconds: int = field(default_factory=lambda: _int("SCAN_BUFFER_SECONDS", 90))
    strict_bar_ts: bool = field(default_factory=lambda: _bool("STRICT_BAR_TS", True))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())
    # Display only: the scan schedule stays anchored to UTC candle boundaries.
    display_tz: str = field(
        default_factory=lambda: _str("DISPLAY_TZ", "Asia/Kuala_Lumpur")
    )
    enable_health_server: bool = field(
        default_factory=lambda: _bool("ENABLE_HEALTH_SERVER", bool(os.environ.get("PORT")))
    )
    port: int = field(default_factory=lambda: _int("PORT", 8080))

    def __post_init__(self) -> None:
        if self.interval not in INTERVAL_SECONDS:
            raise ValueError(
                f"INTERVAL={self.interval!r} unsupported; "
                f"choose one of {sorted(INTERVAL_SECONDS)}"
            )
        if self.macd_fast >= self.macd_slow:
            raise ValueError("MACD_FAST must be smaller than MACD_SLOW")
        if self.mcap_source not in ("gate", "coingecko", "auto"):
            raise ValueError("MCAP_SOURCE must be one of: gate, coingecko, auto")
        if self.concurrency < 1:
            raise ValueError("CONCURRENCY must be >= 1")
        # Validate the regex eagerly so a typo fails at boot, not mid-scan.
        re.compile(self.leveraged_regex, re.IGNORECASE)
        if self.candle_limit < self.min_candles:
            object.__setattr__(self, "candle_limit", self.min_candles + 10)

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS[self.interval]

    @property
    def min_candles(self) -> int:
        """Bars needed before MACD/signal are trustworthy (plus EMA warm-up)."""
        macd_warmup = self.macd_slow + self.macd_signal + 10
        return max(macd_warmup, self.ema_len + 10, 40)

    @property
    def leveraged_pattern(self) -> re.Pattern[str]:
        return re.compile(self.leveraged_regex, re.IGNORECASE)

    @property
    def timeframe_label(self) -> str:
        return self.interval.upper()

    @property
    def tzinfo(self):  # noqa: ANN201 - timezone | ZoneInfo
        from app.timefmt import get_zone

        return get_zone(self.display_tz)


def load_config() -> Config:
    return Config()
