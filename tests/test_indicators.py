import math

import numpy as np
import pytest

from app.indicators import ema, evaluate, macd


def test_ema_is_undefined_until_the_sma_seed():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(values, 3)
    assert all(math.isnan(v) for v in result[:2])
    assert result[2] == pytest.approx(2.0)  # SMA seed of 1,2,3


def test_ema_matches_manual_recursion():
    values = [10.0, 11.0, 12.0, 11.5, 13.0, 12.0]
    length = 3
    alpha = 2 / (length + 1)
    expected = sum(values[:3]) / 3
    for value in values[3:]:
        expected = (value - expected) * alpha + expected
    assert ema(values, length)[-1] == pytest.approx(expected)


def test_ema_of_a_constant_series_is_that_constant():
    assert ema([7.0] * 50, 20)[-1] == pytest.approx(7.0)


def test_macd_of_a_linear_ramp_reaches_steady_state():
    closes = [float(i) for i in range(1, 121)]
    macd_line, signal_line, hist = macd(closes, 12, 26, 9)
    # On a constant-slope series MACD converges to (slow - fast) / 2 = 7.
    assert macd_line[-1] == pytest.approx(7.0, abs=1e-6)
    assert signal_line[-1] == pytest.approx(7.0, abs=1e-6)
    assert hist[-1] == pytest.approx(0.0, abs=1e-6)


def test_macd_signal_starts_where_the_macd_line_starts():
    closes = [float(i) for i in range(1, 61)]
    macd_line, signal_line, _ = macd(closes, 12, 26, 9)
    first_macd = int(np.argmax(~np.isnan(macd_line)))
    first_signal = int(np.argmax(~np.isnan(signal_line)))
    assert first_macd == 25  # slow length - 1
    assert first_signal == first_macd + 8  # signal length - 1 bars later


def test_no_look_ahead_streaming_matches_batch():
    rng = np.random.default_rng(7)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 200)))
    batch_macd, batch_signal, _ = macd(closes, 12, 26, 9)
    batch_ema = ema(closes, 20)
    for cut in (60, 100, 150, 200):
        prefix_macd, prefix_signal, _ = macd(closes[:cut], 12, 26, 9)
        assert prefix_macd[-1] == pytest.approx(batch_macd[cut - 1])
        assert prefix_signal[-1] == pytest.approx(batch_signal[cut - 1])
        assert ema(closes[:cut], 20)[-1] == pytest.approx(batch_ema[cut - 1])


def test_evaluate_returns_none_when_series_is_too_short():
    assert evaluate([1.0, 2.0, 3.0]) is None


def _trigger_series():
    """Dip then rally: produces a cross with MACD above zero and close > EMA20."""
    up = [100 + 3.0 * i for i in range(40)]          # strong uptrend -> MACD >> 0
    dip = [up[-1] - 2.0 * i for i in range(1, 13)]   # pullback -> MACD falls under signal
    rally = [dip[-1] + 6.0 * i for i in range(1, 9)] # sharp recovery -> cross back up
    return up + dip + rally


def test_evaluate_detects_a_golden_cross_above_zero():
    closes = _trigger_series()
    # Walk forward to the first bar where all three conditions hold.
    hit_index = None
    for cut in range(50, len(closes) + 1):
        signal = evaluate(closes[:cut])
        if signal is not None and signal.triggered:
            hit_index = cut
            break
    assert hit_index is not None
    signal = evaluate(closes[:hit_index])
    assert signal.macd_prev <= signal.signal_prev
    assert signal.macd > signal.signal
    assert signal.macd > 0
    assert signal.close > signal.ema


def test_flat_series_never_triggers():
    signal = evaluate([50.0] * 120)
    assert signal is not None
    assert not signal.triggered
    assert not signal.close_above_ema


def test_downtrend_cross_below_zero_is_rejected():
    # Falling market: a MACD cross can happen but stays below zero.
    closes = [200 - 2.0 * i for i in range(60)] + [80 + 0.6 * i for i in range(1, 12)]
    signal = evaluate(closes)
    assert signal is not None
    if signal.golden_cross:
        assert signal.macd < 0
    assert not signal.triggered
