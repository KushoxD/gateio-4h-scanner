"""EMA / MACD computed from a closed-bar close series.

Indicator values at index ``i`` depend only on closes ``0..i``, so there is no
look-ahead: feeding the series one bar at a time yields identical values.

EMA seeding follows the TA-Lib / pandas-ta convention: the first EMA value sits
at index ``length - 1`` and equals the simple moving average of the first
``length`` closes; every later value is the usual recursive update.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NaN = float("nan")


def ema(values: np.ndarray | list[float], length: int) -> np.ndarray:
    """Exponential moving average, NaN-padded until the SMA seed is available."""
    if length < 1:
        raise ValueError("EMA length must be >= 1")
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape[0], NaN, dtype=float)
    if arr.shape[0] < length:
        return out

    alpha = 2.0 / (length + 1.0)
    prev = float(arr[:length].mean())
    out[length - 1] = prev
    for i in range(length, arr.shape[0]):
        prev = (arr[i] - prev) * alpha + prev
        out[i] = prev
    return out


def macd(
    values: np.ndarray | list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(macd_line, signal_line, histogram)`` aligned to ``values``."""
    if fast >= slow:
        raise ValueError("fast length must be smaller than slow length")
    arr = np.asarray(values, dtype=float)
    n = arr.shape[0]
    empty = np.full(n, NaN, dtype=float)

    fast_ema = ema(arr, fast)
    slow_ema = ema(arr, slow)
    macd_line = fast_ema - slow_ema  # NaN wherever either leg is undefined

    valid = ~np.isnan(macd_line)
    if not valid.any():
        return empty, empty.copy(), empty.copy()

    start = int(np.argmax(valid))  # first index where the MACD line exists
    signal_tail = ema(macd_line[start:], signal)
    signal_line = np.full(n, NaN, dtype=float)
    signal_line[start:] = signal_tail

    hist = macd_line - signal_line
    return macd_line, signal_line, hist


@dataclass(frozen=True)
class Signal:
    """Indicator readings for the last closed bar plus the bar before it."""

    close: float
    ema: float
    macd: float
    macd_prev: float
    signal: float
    signal_prev: float

    @property
    def hist(self) -> float:
        return self.macd - self.signal

    @property
    def hist_prev(self) -> float:
        return self.macd_prev - self.signal_prev

    @property
    def golden_cross(self) -> bool:
        """MACD was at/below signal on the prior bar and is above it now."""
        return self.macd_prev <= self.signal_prev and self.macd > self.signal

    @property
    def macd_above_zero(self) -> bool:
        return self.macd > 0.0

    @property
    def close_above_ema(self) -> bool:
        return self.close > self.ema

    @property
    def triggered(self) -> bool:
        return self.golden_cross and self.macd_above_zero and self.close_above_ema


def evaluate(
    closes: np.ndarray | list[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    ema_len: int = 20,
) -> Signal | None:
    """Evaluate the newest bar of ``closes``.

    ``closes`` must contain closed bars only, oldest first. Returns ``None``
    when the series is too short for every indicator to be defined on both the
    newest bar and the one before it.
    """
    arr = np.asarray(closes, dtype=float)
    if arr.shape[0] < 2:
        return None

    macd_line, signal_line, _ = macd(arr, fast=fast, slow=slow, signal=signal)
    ema_line = ema(arr, ema_len)

    i, j = arr.shape[0] - 1, arr.shape[0] - 2
    readings = (
        macd_line[i],
        macd_line[j],
        signal_line[i],
        signal_line[j],
        ema_line[i],
        arr[i],
    )
    if any(np.isnan(value) for value in readings):
        return None

    return Signal(
        close=float(arr[i]),
        ema=float(ema_line[i]),
        macd=float(macd_line[i]),
        macd_prev=float(macd_line[j]),
        signal=float(signal_line[i]),
        signal_prev=float(signal_line[j]),
    )
