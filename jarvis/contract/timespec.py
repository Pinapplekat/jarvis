"""Time words used by job manifests: '15m' intervals and 'HH:MM' clock times."""

from __future__ import annotations

import re
from datetime import time

MIN_EVERY_S = 30
_INTERVAL = re.compile(r"^(\d+)([smh])$")
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_UNIT = {"s": 1, "m": 60, "h": 3600}


def parse_interval(text: object) -> int | None:
    """'30s', '15m' or '6h' as seconds, or None if it isn't one or is under 30 seconds."""
    m = _INTERVAL.match(text) if isinstance(text, str) else None
    seconds = int(m[1]) * _UNIT[m[2]] if m else 0
    return seconds if seconds >= MIN_EVERY_S else None


def parse_clock(text: object) -> time | None:
    """'07:00' as a time of day, or None."""
    m = _CLOCK.match(text) if isinstance(text, str) else None
    return time(int(m[1]), int(m[2])) if m else None
