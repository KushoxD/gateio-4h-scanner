from datetime import timezone

import pytest

from app.config import Config
from app.scanner import last_closed_bar_open, next_close_time
from app.timefmt import fmt, get_zone, offset_seconds, tz_label

FOUR_HOURS = 14400
# 2026-09-11 12:00:00 UTC == 2026-09-11 20:00 GMT+8
BOUNDARY = 1789128000


def test_gmt8_zone_resolves_and_has_no_dst():
    tz = get_zone("Asia/Kuala_Lumpur")
    assert offset_seconds(tz, BOUNDARY) == 8 * 3600
    # Same offset six months later - this zone does not observe DST.
    assert offset_seconds(tz, BOUNDARY + 182 * 86400) == 8 * 3600
    assert tz_label(tz) == "GMT+8"


def test_unknown_zone_falls_back_to_utc():
    tz = get_zone("Mars/Olympus_Mons")
    assert tz is timezone.utc
    assert tz_label(tz) == "UTC"


def test_utc_is_labelled_utc():
    assert tz_label(get_zone("UTC")) == "UTC"
    assert offset_seconds(get_zone("UTC")) == 0


def test_formatting_shifts_the_clock_by_eight_hours():
    tz = get_zone("Asia/Kuala_Lumpur")
    assert fmt(BOUNDARY, tz) == "2026-09-11 20:00 GMT+8"
    assert fmt(BOUNDARY, timezone.utc) == "2026-09-11 12:00 UTC"
    assert fmt(BOUNDARY, tz, with_label=False) == "2026-09-11 20:00"
    assert fmt(None, tz) == "—"


def test_half_hour_offsets_render_with_minutes():
    tz = get_zone("Asia/Kolkata")
    assert tz_label(tz) == "GMT+5:30"


def test_four_hour_boundaries_stay_whole_hours_in_gmt8():
    """GMT+8 is a whole-hour offset, so bar closes land on clean local hours."""
    tz = get_zone("Asia/Kuala_Lumpur")
    seen = set()
    for i in range(6):
        ts = BOUNDARY + i * FOUR_HOURS
        seen.add(fmt(ts, tz, "%H:%M", with_label=False))
    assert seen == {"00:00", "04:00", "08:00", "12:00", "16:00", "20:00"}


def test_display_tz_does_not_move_the_schedule(monkeypatch):
    """Changing DISPLAY_TZ must not shift which bar is scanned, or when."""
    monkeypatch.setenv("DISPLAY_TZ", "UTC")
    utc_cfg = Config()
    monkeypatch.setenv("DISPLAY_TZ", "Asia/Kuala_Lumpur")
    kl_cfg = Config()

    now = BOUNDARY + 90
    assert utc_cfg.interval_seconds == kl_cfg.interval_seconds
    assert last_closed_bar_open(now, utc_cfg.interval_seconds) == last_closed_bar_open(
        now, kl_cfg.interval_seconds
    )
    assert next_close_time(now, utc_cfg.interval_seconds) == next_close_time(
        now, kl_cfg.interval_seconds
    )


def test_config_default_is_gmt8(monkeypatch):
    monkeypatch.delenv("DISPLAY_TZ", raising=False)
    cfg = Config()
    assert cfg.display_tz == "Asia/Kuala_Lumpur"
    assert offset_seconds(cfg.tzinfo, BOUNDARY) == 8 * 3600
