"""Derive EGX's trading calendar from the exchange's own trading record.

The exchange's calendar and Egypt's public holidays are not the same list, and
treating them as the same produced real bugs. Measured against the record: EGX
was OPEN on 2026-01-25 (Police Day) and 2026-06-30 (June 30 Revolution), yet
CLOSED on both of those dates in 2024. The same nominal holiday is a trading day
in one year and not the next, so no rule derived from the public-holiday list can
be correct — and Egypt's Islamic and Coptic observances move every year anyway.

So the calendar is inferred instead: a date is closed when almost none of a
basket of liquid, index-weighted names has a bar for it. The feed drops roughly
22-30% of sessions at random, so one name missing a day means nothing; the whole
basket missing the same day does not happen by chance.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Final

from egx_trader.market_calendar.egx import EGXCalendar

# A real trading day leaves ~70-78% of the basket with bars. Requiring exactly
# zero is too brittle — a single phantom bar defeats it, which is what let
# 2026-07-23 (July 23 Revolution) through as a trading day. At 15%, declaring a
# day closed needs almost the entire basket to be missing simultaneously, which
# independent 25% loss will not produce.
CLOSED_THRESHOLD: Final = 0.15

# A day the calendar calls closed but this fraction of the basket traded through
# is a false holiday. Set well above CLOSED_THRESHOLD so ambiguous days are left
# alone rather than flipped on weak evidence.
OPEN_THRESHOLD: Final = 0.50

_TRADING_WEEKDAYS: Final[frozenset[int]] = frozenset({6, 0, 1, 2, 3})


@dataclass(frozen=True, slots=True)
class CalendarEvidence:
    """Per-date evidence, and what it disagrees with the calendar about."""

    basket_size: int
    traded_fraction: dict[dt.date, float]
    closed: list[dt.date] = field(default_factory=list)
    """Weekdays where almost nothing traded: holidays."""

    false_holidays: list[tuple[dt.date, float]] = field(default_factory=list)
    """Dates the calendar calls closed that the market clearly traded through."""

    ambiguous: list[tuple[dt.date, float]] = field(default_factory=list)
    """Between the thresholds — too weak to act on either way."""


def build_evidence(
    presence: dict[dt.date, int],
    basket_size: int,
    calendar: EGXCalendar,
    start: dt.date,
    end: dt.date,
) -> CalendarEvidence:
    """Classify each weekday in range from how much of the basket traded."""
    fractions: dict[dt.date, float] = {}
    closed: list[dt.date] = []
    false_holidays: list[tuple[dt.date, float]] = []
    ambiguous: list[tuple[dt.date, float]] = []

    day = start
    while day <= end:
        if day.weekday() in _TRADING_WEEKDAYS:
            fraction = presence.get(day, 0) / basket_size if basket_size else 0.0
            fractions[day] = fraction
            marked_open = calendar.is_trading_day(day)

            if fraction < CLOSED_THRESHOLD:
                closed.append(day)
            elif not marked_open and fraction >= OPEN_THRESHOLD:
                false_holidays.append((day, fraction))
            elif not marked_open:
                ambiguous.append((day, fraction))
        day += dt.timedelta(days=1)

    return CalendarEvidence(
        basket_size=basket_size,
        traded_fraction=fractions,
        closed=closed,
        false_holidays=false_holidays,
        ambiguous=ambiguous,
    )


def collect_presence(
    symbols: list[str],
    fetch: Callable[[str], set[dt.date] | None],
    max_workers: int = 6,
) -> tuple[dict[dt.date, int], int]:
    """Count, per date, how many symbols have a bar. Returns (counts, basket size).

    `fetch` returns the dates a symbol has bars for, or None if it is unavailable
    upstream. Unavailable symbols shrink the basket rather than counting as a
    market-wide absence — otherwise a delisted name would fake a holiday.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = [dates for dates in pool.map(fetch, symbols) if dates]

    presence: dict[dt.date, int] = defaultdict(int)
    for dates in results:
        for day in dates:
            presence[day] += 1
    return dict(presence), len(results)


# Named only where a date maps unambiguously onto a known Egyptian observance.
# Everything else stays generic — inventing a holiday name would be fabrication.
FIXED_OBSERVANCES: Final[dict[tuple[int, int], str]] = {
    (1, 1): "New Year's Day",
    (1, 7): "Coptic Christmas",
    (1, 25): "Revolution Day / National Police Day",
    (4, 25): "Sinai Liberation Day",
    (5, 1): "Labour Day",
    (6, 30): "June 30 Revolution",
    (7, 23): "July 23 Revolution",
    (10, 6): "Armed Forces Day",
}

UNIDENTIFIED: Final = "EGX closed (observance not identified)"


def label_for(day: dt.date) -> str:
    return FIXED_OBSERVANCES.get((day.month, day.day), UNIDENTIFIED)
