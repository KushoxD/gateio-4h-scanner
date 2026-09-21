"""Timezone handling for everything a human reads.

The scan schedule itself stays anchored to UTC, because Gate.io's candles are:
a 4H bar always opens at 00/04/08/12/16/20 UTC. ``DISPLAY_TZ`` only changes how
those instants are *written* — in GMT+8 the same boundaries simply read as
08:00, 12:00, 16:00, 20:00, 00:00 and 04:00 local.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

DEFAULT_TZ = "Asia/Kuala_Lumpur"


def get_zone(name: str) -> timezone | ZoneInfo:
    """Resolve an IANA zone name, falling back to UTC if it is unavailable."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("DISPLAY_TZ=%r is not a known timezone; falling back to UTC", name)
        return timezone.utc


def offset_seconds(tz: timezone | ZoneInfo, ts: float | None = None) -> int:
    """UTC offset of ``tz`` at ``ts`` (default: now), in seconds.

    The instant matters: zones change their offset over history, and a few
    observe DST. Kuala Lumpur, for one, was GMT+7:30 at the epoch.
    """
    moment = datetime.fromtimestamp(
        ts if ts is not None else time.time(), tz=timezone.utc
    )
    delta = moment.astimezone(tz).utcoffset() or timedelta(0)
    return int(delta.total_seconds())


def tz_label(tz: timezone | ZoneInfo, ts: float | None = None) -> str:
    """A short human label such as ``GMT+8`` or ``GMT-3:30``."""
    total = offset_seconds(tz, ts)
    if total == 0:
        return "UTC"
    sign = "+" if total > 0 else "-"
    total = abs(total)
    hours, minutes = divmod(total // 60, 60)
    return f"GMT{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def fmt(
    ts: float | None,
    tz: timezone | ZoneInfo,
    pattern: str = "%Y-%m-%d %H:%M",
    *,
    with_label: bool = True,
) -> str:
    """Render an epoch timestamp in ``tz``."""
    if ts is None:
        return "—"
    text = datetime.fromtimestamp(ts, tz=tz).strftime(pattern)
    return f"{text} {tz_label(tz, ts)}" if with_label else text


class ZonedFormatter(logging.Formatter):
    """Log formatter that stamps records in a chosen timezone."""

    def __init__(self, fmt_str: str, tz: timezone | ZoneInfo) -> None:
        super().__init__(fmt_str)
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        moment = datetime.fromtimestamp(record.created, tz=self._tz)
        return moment.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")
