from __future__ import annotations

import math
import re
from datetime import timedelta

SECONDS_PER_DAY = 24 * 60 * 60
SECONDS_PER_YEAR = 365 * SECONDS_PER_DAY

_BAR_PATTERN = re.compile(r"^(?P<count>\d+)(?P<unit>[mHDWM])(?:utc)?$")
_UNIT_TO_SECONDS = {
    "m": 60,
    "H": 60 * 60,
    "D": SECONDS_PER_DAY,
    "W": 7 * SECONDS_PER_DAY,
    "M": 30 * SECONDS_PER_DAY,
}


def bar_seconds(bar: str) -> int:
    match = _BAR_PATTERN.fullmatch(bar.strip())
    if match is None:
        raise ValueError(f"Unsupported OKX bar: {bar}")
    return int(match.group("count")) * _UNIT_TO_SECONDS[match.group("unit")]


def bar_timedelta(bar: str, bars: int = 1) -> timedelta:
    return timedelta(seconds=bar_seconds(bar) * bars)


def bars_per_year(bar: str) -> float:
    return SECONDS_PER_YEAR / bar_seconds(bar)


def bars_for_days(bar: str, days: int) -> int:
    return max(1, math.ceil(days * SECONDS_PER_DAY / bar_seconds(bar)))
