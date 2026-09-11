"""Telegram alert delivery."""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram's hard limit is 4096; leave headroom.


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        dry_run: bool = False,
        timeout: float = 20.0,
        max_retries: int = 4,
        api_base: str = TELEGRAM_API,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_base = api_base.rstrip("/")
        self.dry_run = dry_run or not (bot_token and chat_id)
        if self.dry_run and not dry_run:
            log.warning(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - alerts will only be logged"
            )
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return not self.dry_run

    def send(self, text: str) -> bool:
        if self.dry_run:
            log.info("[DRY RUN] would send Telegram message:\n%s", text)
            return True

        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    log.error("Telegram send failed: %s", exc)
                    return False
                time.sleep(min(2**attempt, 20))
                continue

            if response.status_code == 429:
                retry_after = 5.0
                try:
                    retry_after = float(
                        response.json().get("parameters", {}).get("retry_after", 5)
                    )
                except (ValueError, AttributeError):
                    pass
                if attempt >= self.max_retries:
                    log.error("Telegram rate limited, giving up on this message")
                    return False
                log.warning("Telegram rate limited, sleeping %.1fs", retry_after)
                time.sleep(min(retry_after, 60))
                continue

            if response.status_code >= 500:
                if attempt >= self.max_retries:
                    log.error("Telegram server error %s", response.status_code)
                    return False
                time.sleep(min(2**attempt, 20))
                continue

            if not response.ok:
                log.error(
                    "Telegram rejected the message (HTTP %s): %s",
                    response.status_code,
                    response.text[:300],
                )
                return False

            return True
        return False

    def close(self) -> None:
        self._session.close()


def _fmt_price(value: float) -> str:
    """Format a price with enough precision for sub-cent altcoins."""
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.4f}"
    if magnitude >= 0.01:
        return f"{value:.6f}"
    return f"{value:.10f}".rstrip("0").rstrip(".")


def _fmt_indicator(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1:
        return f"{value:,.4f}"
    if magnitude >= 0.0001:
        return f"{value:.8f}"
    return f"{value:.3e}"


def _fmt_usd(value: float) -> str:
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.2f}M"
    if value >= 1e3:
        return f"${value / 1e3:.2f}K"
    return f"${value:,.0f}"


def format_alert(hit: Any) -> str:
    """Render a :class:`app.scanner.Hit` as a Telegram HTML message."""
    pair = html.escape(hit.pair)
    url = f"https://www.gate.io/trade/{hit.pair}"
    mcap = _fmt_usd(hit.market_cap) if hit.market_cap > 0 else "n/a"
    volume = _fmt_usd(hit.quote_volume_24h) if hit.quote_volume_24h > 0 else "n/a"

    return "\n".join(
        [
            f"🚀 <b>MACD golden cross above zero</b> — <b>{pair}</b>",
            "",
            f"Exchange: <b>Gate</b>   Timeframe: <b>{html.escape(hit.timeframe)}</b>",
            f"Bar close (UTC): <code>{html.escape(hit.bar_close_utc)}</code>",
            "",
            f"Close: <code>{_fmt_price(hit.close)}</code>",
            f"EMA{hit.ema_len}: <code>{_fmt_price(hit.ema)}</code>",
            f"MACD: <code>{_fmt_indicator(hit.macd)}</code>",
            f"Signal: <code>{_fmt_indicator(hit.signal)}</code>",
            f"Hist: <code>{_fmt_indicator(hit.macd - hit.signal)}</code>",
            "",
            f"Market cap: <b>{mcap}</b>",
            f"24h quote vol: <b>{volume}</b>",
            "",
            f'<a href="{url}">{url}</a>',
        ]
    )
