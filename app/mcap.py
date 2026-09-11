"""Market-cap lookup for base assets.

Gate.io's ``/spot/currencies`` does not reliably carry a market cap, so the
default ``MCAP_SOURCE=auto`` reads whatever Gate exposes and fills the gaps
from CoinGecko's public ``/coins/markets`` endpoint.

Symbols are not unique on CoinGecko. When several coins share a ticker we keep
the LARGEST market cap for that symbol, which biases the ``MIN_MCAP`` screen
toward inclusion rather than silently dropping a legitimate large-cap coin.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

# Keys Gate has used (or may use) for a market cap on /spot/currencies.
_GATE_MCAP_KEYS = ("market_cap", "marketcap", "market_cap_usd", "mc")


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0 else 0.0


def gate_market_caps(currencies: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Extract usable market caps from a ``/spot/currencies`` payload."""
    caps: dict[str, float] = {}
    for entry in currencies:
        symbol = str(entry.get("currency", "")).upper()
        if not symbol:
            continue
        for key in _GATE_MCAP_KEYS:
            if key in entry:
                value = _to_float(entry[key])
                if value > 0:
                    caps[symbol] = max(caps.get(symbol, 0.0), value)
                break
    return caps


def coingecko_market_caps(
    *,
    base_url: str = "https://api.coingecko.com/api/v3",
    pages: int = 8,
    api_key: str = "",
    timeout: float = 30.0,
    max_retries: int = 4,
    page_pause: float = 1.5,
) -> dict[str, float]:
    """Top ``pages * 250`` coins by market cap, keyed by upper-case symbol."""
    session = requests.Session()
    session.headers.update(
        {"Accept": "application/json", "User-Agent": "gateio-4h-scanner/1.0"}
    )
    if api_key:
        # Demo keys use x-cg-demo-api-key; pro keys use x-cg-pro-api-key.
        header = "x-cg-pro-api-key" if "pro-api" in base_url else "x-cg-demo-api-key"
        session.headers[header] = api_key

    caps: dict[str, float] = {}
    try:
        for page in range(1, max(1, pages) + 1):
            rows = _coingecko_page(session, base_url, page, timeout, max_retries)
            if not rows:
                break
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                value = _to_float(row.get("market_cap"))
                if symbol and value > 0:
                    caps[symbol] = max(caps.get(symbol, 0.0), value)
            if len(rows) < 250:
                break
            if page_pause > 0:
                time.sleep(page_pause)  # free tier is aggressively rate limited
    finally:
        session.close()

    log.info("CoinGecko market caps loaded for %d symbols", len(caps))
    return caps


def _coingecko_page(
    session: requests.Session,
    base_url: str,
    page: int,
    timeout: float,
    max_retries: int,
) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 250,
        "page": page,
        "sparkline": "false",
    }
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt >= max_retries:
                log.warning("CoinGecko page %d unreachable: %s", page, exc)
                return []
            time.sleep(min(2**attempt, 20))
            continue

        if response.status_code in (429, 500, 502, 503, 504):
            if attempt >= max_retries:
                log.warning("CoinGecko page %d gave HTTP %s", page, response.status_code)
                return []
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            time.sleep(min(delay, 30))
            continue

        if not response.ok:
            log.warning(
                "CoinGecko page %d gave HTTP %s: %s",
                page,
                response.status_code,
                response.text[:200],
            )
            return []

        try:
            data = response.json()
        except ValueError:
            log.warning("CoinGecko page %d returned non-JSON", page)
            return []
        return data if isinstance(data, list) else []
    return []


def build_market_cap_map(
    source: str,
    gate_currencies: Iterable[dict[str, Any]],
    *,
    coingecko_base: str,
    coingecko_pages: int,
    coingecko_api_key: str,
) -> dict[str, float]:
    """Resolve the configured ``MCAP_SOURCE`` into one symbol -> USD cap map."""
    caps: dict[str, float] = {}

    if source in ("gate", "auto"):
        caps = gate_market_caps(gate_currencies)
        log.info("Gate supplied market caps for %d symbols", len(caps))
        if source == "gate":
            return caps

    if source == "coingecko":
        return coingecko_market_caps(
            base_url=coingecko_base,
            pages=coingecko_pages,
            api_key=coingecko_api_key,
        )

    # auto: Gate first, CoinGecko fills anything Gate did not provide.
    fallback = coingecko_market_caps(
        base_url=coingecko_base,
        pages=coingecko_pages,
        api_key=coingecko_api_key,
    )
    for symbol, value in fallback.items():
        caps.setdefault(symbol, value)
    return caps
