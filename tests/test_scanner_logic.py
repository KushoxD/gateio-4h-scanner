import time

import pytest

from app.config import Config
from app.gate import Candle
from app.scanner import (
    closed_candles,
    current_bar_open,
    is_leveraged,
    last_closed_bar_open,
    next_close_time,
)

FOUR_HOURS = 14400
# 2026-09-11 12:00:00 UTC, exactly on a 4H boundary.
BOUNDARY = 1789041600


@pytest.fixture()
def cfg(monkeypatch):
    for key in list(("QUOTE", "INTERVAL", "LEVERAGED_REGEX", "MIN_MCAP")):
        monkeypatch.delenv(key, raising=False)
    return Config()


def test_bar_boundaries_are_aligned_to_utc(cfg):
    assert BOUNDARY % FOUR_HOURS == 0
    assert current_bar_open(BOUNDARY + 90, FOUR_HOURS) == BOUNDARY
    assert last_closed_bar_open(BOUNDARY + 90, FOUR_HOURS) == BOUNDARY - FOUR_HOURS
    assert next_close_time(BOUNDARY + 90, FOUR_HOURS) == BOUNDARY + FOUR_HOURS


def test_exactly_on_the_boundary_the_new_bar_is_forming(cfg):
    assert current_bar_open(BOUNDARY, FOUR_HOURS) == BOUNDARY
    assert last_closed_bar_open(BOUNDARY, FOUR_HOURS) == BOUNDARY - FOUR_HOURS


def _candle(ts, close=1.0, window_closed=None):
    return Candle(
        timestamp=ts,
        quote_volume=1000.0,
        close=close,
        high=close,
        low=close,
        open=close,
        window_closed=window_closed,
    )


def test_forming_candle_is_dropped_by_timestamp():
    now = BOUNDARY + 90
    candles = [_candle(BOUNDARY - 2 * FOUR_HOURS), _candle(BOUNDARY - FOUR_HOURS), _candle(BOUNDARY)]
    kept = closed_candles(candles, now=now, interval_seconds=FOUR_HOURS)
    assert [c.timestamp for c in kept] == [BOUNDARY - 2 * FOUR_HOURS, BOUNDARY - FOUR_HOURS]


def test_window_closed_false_is_dropped_even_if_timestamp_looks_old():
    now = BOUNDARY + FOUR_HOURS
    candles = [_candle(BOUNDARY - FOUR_HOURS, window_closed=True), _candle(BOUNDARY, window_closed=False)]
    kept = closed_candles(candles, now=now, interval_seconds=FOUR_HOURS)
    assert [c.timestamp for c in kept] == [BOUNDARY - FOUR_HOURS]


def test_six_field_rows_without_the_flag_are_kept_when_closed():
    now = BOUNDARY + 90
    candles = [_candle(BOUNDARY - FOUR_HOURS, window_closed=None)]
    assert len(closed_candles(candles, now=now, interval_seconds=FOUR_HOURS)) == 1


@pytest.mark.parametrize("base", ["BTC3L", "BTC3S", "ETH5L", "ETH5S", "XRP4L", "XRP4S", "SOL3L"])
def test_leveraged_tokens_are_excluded(base, cfg):
    assert is_leveraged(base, cfg)


@pytest.mark.parametrize("base", ["BTC", "ETH", "SOL", "GT", "PEPE", "ARB", "SUI", "TIA"])
def test_normal_bases_are_kept(base, cfg):
    assert not is_leveraged(base, cfg)


@pytest.mark.parametrize("base", ["JUP", "SYRUP", "BULL", "UP", "DOWN", "3L", "5S"])
def test_default_pattern_does_not_eat_legitimate_tickers(base, cfg):
    """BULL/BEAR/UP/DOWN are NOT in the default pattern on purpose.

    JUP and SYRUP end in "UP"; a bare "3L"/"5S" ticker has no base prefix.
    Gate names its own leveraged tokens BTC3L / ETH5S, which always do.
    """
    assert not is_leveraged(base, cfg)


def test_leveraged_regex_is_configurable(monkeypatch):
    monkeypatch.setenv("LEVERAGED_REGEX", r".+(?:3|4|5)(?:L|S)$|.{3,}(?:BULL|BEAR|UP|DOWN)$")
    extended = Config()
    assert is_leveraged("BTC3L", extended)
    assert is_leveraged("BTCBULL", extended)
    assert is_leveraged("LINKUP", extended)
    assert not is_leveraged("JUP", extended)  # only "J" precedes UP
    assert not is_leveraged("BTC", extended)


def test_candle_row_parsing_handles_both_response_shapes():
    six = Candle.from_row(["1789041600", "1234.5", "2.0", "2.1", "1.9", "1.95"])
    assert six.timestamp == 1789041600 and six.close == 2.0 and six.window_closed is None

    eight = Candle.from_row(["1789041600", "1234.5", "2.0", "2.1", "1.9", "1.95", "600.0", "true"])
    assert eight.base_volume == 600.0 and eight.window_closed is True

    still_open = Candle.from_row(["1789041600", "1.0", "2.0", "2.1", "1.9", "1.95", "1.0", "false"])
    assert still_open.window_closed is False


def test_short_rows_are_rejected():
    with pytest.raises(ValueError):
        Candle.from_row(["1789041600", "1.0", "2.0"])
