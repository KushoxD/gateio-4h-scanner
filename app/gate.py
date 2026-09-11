"""Minimal Gate.io public REST client.

Only the four public spot endpoints the scanner needs, with a shared rate
limiter, a concurrency cap, and retries with exponential backoff on 429/5xx.
No API key is required for any of these.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence, TypeVar

import requests

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class GateError(RuntimeError):
    """A Gate.io request failed after exhausting retries."""


@dataclass(frozen=True)
class Candle:
    """One Gate.io spot candlestick.

    ``timestamp`` is the bar's OPEN time in epoch seconds. ``window_closed`` is
    Gate's own flag and is ``None`` on the older six-field response shape.
    """

    timestamp: int
    quote_volume: float
    close: float
    high: float
    low: float
    open: float
    base_volume: float | None = None
    window_closed: bool | None = None

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "Candle":
        # Documented order: [ts, quote_volume, close, high, low, open]
        # Newer responses append [base_volume, window_closed].
        if len(row) < 6:
            raise ValueError(f"unexpected candlestick row: {row!r}")

        closed: bool | None = None
        if len(row) >= 8:
            raw = row[7]
            if isinstance(raw, bool):
                closed = raw
            elif isinstance(raw, str):
                closed = raw.strip().lower() == "true"

        return cls(
            timestamp=int(float(row[0])),
            quote_volume=float(row[1]),
            close=float(row[2]),
            high=float(row[3]),
            low=float(row[4]),
            open=float(row[5]),
            base_volume=float(row[6]) if len(row) >= 7 else None,
            window_closed=closed,
        )


class RateLimiter:
    """Enforces a minimum wall-clock gap between outbound requests."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self._min_interval
        if wait > 0:
            time.sleep(wait)


class GateClient:
    def __init__(
        self,
        base_url: str = "https://api.gateio.ws/api/v4",
        *,
        timeout: float = 20.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        request_interval: float = 0.06,
        concurrency: int = 8,
        user_agent: str = "gateio-4h-scanner/1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.concurrency = max(1, concurrency)
        self.user_agent = user_agent
        self._limiter = RateLimiter(request_interval)
        self._local = threading.local()

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {"Accept": "application/json", "User-Agent": self.user_agent}
            )
            self._local.session = session
        return session

    def _sleep_for_retry(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), self.backoff_cap))
                return
            except ValueError:
                pass
        delay = min(self.backoff_base * (2**attempt), self.backoff_cap)
        time.sleep(delay * (0.5 + random.random() / 2.0))  # jitter

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error: str = "unknown error"

        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._session().get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    break
                log.warning("GET %s failed (%s), retrying", path, last_error)
                self._sleep_for_retry(attempt, None)
                continue

            if response.status_code in RETRY_STATUSES:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if attempt >= self.max_retries:
                    break
                log.warning("GET %s -> %s, backing off", path, response.status_code)
                self._sleep_for_retry(attempt, response.headers.get("Retry-After"))
                continue

            if not response.ok:
                raise GateError(
                    f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise GateError(f"GET {path} returned non-JSON body") from exc

        raise GateError(f"GET {path} failed after {self.max_retries + 1} attempts: {last_error}")

    # ------------------------------------------------------------- endpoints
    def currency_pairs(self) -> list[dict[str, Any]]:
        data = self.get("/spot/currency_pairs")
        return data if isinstance(data, list) else []

    def currencies(self) -> list[dict[str, Any]]:
        data = self.get("/spot/currencies")
        return data if isinstance(data, list) else []

    def tickers(self, currency_pair: str | None = None) -> list[dict[str, Any]]:
        params = {"currency_pair": currency_pair} if currency_pair else None
        data = self.get("/spot/tickers", params=params)
        return data if isinstance(data, list) else []

    def candlesticks(
        self, currency_pair: str, interval: str = "4h", limit: int = 80
    ) -> list[Candle]:
        data = self.get(
            "/spot/candlesticks",
            params={"currency_pair": currency_pair, "interval": interval, "limit": limit},
        )
        if not isinstance(data, list):
            return []
        candles = [Candle.from_row(row) for row in data if isinstance(row, list)]
        candles.sort(key=lambda c: c.timestamp)  # Gate returns oldest-first; be explicit
        return candles

    # ------------------------------------------------------------ concurrency
    def map(
        self, func: Callable[[T], R], items: Iterable[T]
    ) -> list[tuple[T, R | None, Exception | None]]:
        """Run ``func`` over ``items`` under the client's concurrency cap.

        Returns ``(item, result, exception)`` triples in input order; a failing
        item never aborts the batch.
        """
        items = list(items)
        if not items:
            return []
        results: list[tuple[T, R | None, Exception | None]] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(func, item) for item in items]
            for item, future in zip(items, futures):
                try:
                    results.append((item, future.result(), None))
                except Exception as exc:  # noqa: BLE001 - reported per item
                    results.append((item, None, exc))
        return results

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None
