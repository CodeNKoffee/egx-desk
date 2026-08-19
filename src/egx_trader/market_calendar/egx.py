"""EGX trading calendar and session clock.

Everything here is Africa/Cairo. Naive datetimes are rejected rather than assumed
to be local — a timezone mistake in a market clock is silent and expensive.

The default session (EGX regular trading):

    09:30 ─ 10:00   discovery / pre-open, with a RANDOM close between 09:50-10:00
    10:00 ─ 14:15   continuous trading
    14:15 ─ 14:25   closing auction
    14:25 ─ 14:30   trading at closing price

Trading days are Sunday through Thursday.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import yaml

CAIRO: Final = ZoneInfo("Africa/Cairo")

_DEFAULT_HOLIDAYS_FILE: Final = Path(__file__).parent / "holidays.yaml"

# Python's weekday(): Mon=0 … Sun=6. EGX trades Sunday through Thursday.
_TRADING_WEEKDAYS: Final[frozenset[int]] = frozenset({6, 0, 1, 2, 3})


class CalendarCoverageError(RuntimeError):
    """Asked about a date the holiday file does not vouch for.

    Raised instead of guessing. See the header of holidays.yaml for why.
    """


class SessionPhase(StrEnum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"
    CONTINUOUS = "continuous"
    CLOSING_AUCTION = "closing_auction"
    TRADING_AT_CLOSE = "trading_at_close"

    @property
    def accepts_orders(self) -> bool:
        """Whether an order can be submitted during this phase."""
        return self is not SessionPhase.CLOSED


@dataclass(frozen=True, slots=True)
class SessionSchedule:
    """Phase boundaries for one trading day."""

    pre_open_start: dt.time = dt.time(9, 30)
    continuous_start: dt.time = dt.time(10, 0)
    continuous_end: dt.time = dt.time(14, 15)
    closing_auction_end: dt.time = dt.time(14, 25)
    session_end: dt.time = dt.time(14, 30)

    # The discovery session closes at a random moment in this window, so orders
    # placed after `pre_open_random_close_start` may or may not be accepted.
    pre_open_random_close_start: dt.time = dt.time(9, 50)

    def phase_at(self, t: dt.time) -> SessionPhase:
        if t < self.pre_open_start:
            return SessionPhase.CLOSED
        if t < self.continuous_start:
            return SessionPhase.PRE_OPEN
        if t < self.continuous_end:
            return SessionPhase.CONTINUOUS
        if t < self.closing_auction_end:
            return SessionPhase.CLOSING_AUCTION
        if t < self.session_end:
            return SessionPhase.TRADING_AT_CLOSE
        return SessionPhase.CLOSED


DEFAULT_SCHEDULE: Final = SessionSchedule()


@dataclass(frozen=True, slots=True)
class SpecialSession:
    """An inclusive date range running on non-default hours (e.g. Ramadan)."""

    start: dt.date
    end: dt.date
    reason: str
    schedule: SessionSchedule

    def covers(self, d: dt.date) -> bool:
        return self.start <= d <= self.end


def _parse_time(raw: str) -> dt.time:
    hours, minutes = raw.split(":")
    return dt.time(int(hours), int(minutes))


class EGXCalendar:
    """Trading-day and session-phase lookups for the Egyptian Exchange.

    `strict=True` (the default) makes date queries past the holiday file's
    `verified_through` raise `CalendarCoverageError`. Live code should keep it on.
    Backtests may pass `strict=False`: over history, holidays are self-evident
    because no bars exist for them.
    """

    def __init__(
        self,
        holidays_file: Path | None = None,
        *,
        strict: bool = True,
    ) -> None:
        self._strict = strict
        raw = yaml.safe_load((holidays_file or _DEFAULT_HOLIDAYS_FILE).read_text())

        self._verified_through: dt.date = raw["verified_through"]
        self._holidays: dict[dt.date, str] = dict(raw.get("holidays") or {})
        self._special: list[SpecialSession] = [
            SpecialSession(
                start=entry["start"],
                end=entry["end"],
                reason=entry["reason"],
                schedule=SessionSchedule(
                    pre_open_start=_parse_time(entry["pre_open_start"]),
                    continuous_start=_parse_time(entry["continuous_start"]),
                    continuous_end=_parse_time(entry["continuous_end"]),
                    closing_auction_end=_parse_time(entry["closing_auction_end"]),
                    session_end=_parse_time(entry["session_end"]),
                ),
            )
            for entry in (raw.get("special_sessions") or [])
        ]

    # ── Coverage ─────────────────────────────────────────────────────────────

    @property
    def verified_through(self) -> dt.date:
        return self._verified_through

    def _check_coverage(self, d: dt.date) -> None:
        if self._strict and d > self._verified_through:
            raise CalendarCoverageError(
                f"{d} is past the calendar's verified_through ({self._verified_through}). "
                "Add EGX's published non-trading days to holidays.yaml and extend "
                "verified_through. Refusing to guess: a missing holiday corrupts "
                "settlement-date math."
            )

    # ── Trading days ─────────────────────────────────────────────────────────

    def is_trading_day(self, d: dt.date) -> bool:
        self._check_coverage(d)
        return d.weekday() in _TRADING_WEEKDAYS and d not in self._holidays

    def holiday_name(self, d: dt.date) -> str | None:
        return self._holidays.get(d)

    @property
    def holiday_count(self) -> int:
        """How many non-trading days the file records, for diagnostics."""
        return len(self._holidays)

    def schedule_for(self, d: dt.date) -> SessionSchedule:
        for special in self._special:
            if special.covers(d):
                return special.schedule
        return DEFAULT_SCHEDULE

    # ── Session phase ────────────────────────────────────────────────────────

    def session_phase(self, when: dt.datetime) -> SessionPhase:
        """Phase at `when`. Requires an aware datetime; converts to Cairo."""
        if when.tzinfo is None:
            raise ValueError(
                "session_phase() requires a timezone-aware datetime. "
                "Pass one in UTC or Cairo rather than relying on the host clock."
            )
        local = when.astimezone(CAIRO)
        if not self.is_trading_day(local.date()):
            return SessionPhase.CLOSED
        return self.schedule_for(local.date()).phase_at(local.time())

    def is_open(self, when: dt.datetime) -> bool:
        """True when orders can be submitted (pre-open through trading-at-close)."""
        return self.session_phase(when).accepts_orders

    def in_pre_open_random_close(self, when: dt.datetime) -> bool:
        """True inside the 09:50-10:00 window where discovery may close at any moment.

        Order placement here is unreliable by design — the exchange picks the
        cutoff randomly. Treat a rejection in this window as expected, not a bug.
        """
        if self.session_phase(when) is not SessionPhase.PRE_OPEN:
            return False
        local = when.astimezone(CAIRO)
        return local.time() >= self.schedule_for(local.date()).pre_open_random_close_start

    # ── Session arithmetic (settlement math depends on this) ─────────────────

    def next_session(self, d: dt.date) -> dt.date:
        """The first trading day strictly after `d`."""
        candidate = d + dt.timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += dt.timedelta(days=1)
        return candidate

    def previous_session(self, d: dt.date) -> dt.date:
        """The last trading day strictly before `d`."""
        candidate = d - dt.timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate -= dt.timedelta(days=1)
        return candidate

    def add_sessions(self, d: dt.date, n: int) -> dt.date:
        """Advance `n` trading sessions from `d`.

        This is how settlement dates are computed: a T+2 lot bought on `d` settles
        on `add_sessions(d, 2)`. `n=0` returns `d` itself if it is a trading day,
        otherwise the next one.
        """
        if n < 0:
            raise ValueError("add_sessions() does not go backwards; use previous_session()")
        current = d
        if not self.is_trading_day(current):
            current = self.next_session(current)
        for _ in range(n):
            current = self.next_session(current)
        return current

    def sessions_between(self, start: dt.date, end: dt.date) -> int:
        """Count trading sessions in the half-open interval [start, end).

        Zero when `end <= start`.
        """
        if end <= start:
            return 0
        count = 0
        current = start
        while current < end:
            if self.is_trading_day(current):
                count += 1
            current += dt.timedelta(days=1)
        return count

    def trading_days_in(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """Every trading day in the inclusive range [start, end]."""
        days: list[dt.date] = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += dt.timedelta(days=1)
        return days
