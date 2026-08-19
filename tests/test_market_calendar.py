"""Calendar tests.

Settlement math rides on `add_sessions`/`sessions_between`, so those get the most
attention: getting them wrong makes the system think an unsettled lot is sellable.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from egx_trader.market_calendar import (
    CAIRO,
    CalendarCoverageError,
    EGXCalendar,
    SessionPhase,
)

UTC = ZoneInfo("UTC")


@pytest.fixture
def cal() -> EGXCalendar:
    return EGXCalendar()


@pytest.fixture
def loose_cal() -> EGXCalendar:
    """Non-strict, for dates past `verified_through`."""
    return EGXCalendar(strict=False)


def cairo(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=CAIRO)


# ── Trading days ─────────────────────────────────────────────────────────────


class TestTradingDays:
    def test_sunday_through_thursday_trade(self, cal: EGXCalendar) -> None:
        # 2026-08-02 is a Sunday.
        for offset, expected in enumerate([True, True, True, True, True, False, False]):
            day = dt.date(2026, 8, 2) + dt.timedelta(days=offset)
            assert cal.is_trading_day(day) is expected, f"{day} ({day:%a})"

    def test_friday_and_saturday_are_weekend(self, cal: EGXCalendar) -> None:
        assert cal.is_trading_day(dt.date(2026, 8, 7)) is False  # Friday
        assert cal.is_trading_day(dt.date(2026, 8, 8)) is False  # Saturday

    @pytest.mark.parametrize(
        "day,name",
        [
            (dt.date(2026, 1, 1), "New Year's Day"),  # Thursday
            (dt.date(2026, 1, 7), "Coptic Christmas"),  # Wednesday
            (dt.date(2026, 7, 23), "July 23 Revolution"),  # Thursday
            (dt.date(2024, 1, 25), "Revolution Day / National Police Day"),  # Thursday
        ],
    )
    def test_holidays_on_trading_weekdays_are_closed(
        self, cal: EGXCalendar, day: dt.date, name: str
    ) -> None:
        assert day.weekday() in {6, 0, 1, 2, 3}, "test case must be a normal trading weekday"
        assert cal.is_trading_day(day) is False
        assert cal.holiday_name(day) == name


# ── Coverage guard ───────────────────────────────────────────────────────────


class TestCoverage:
    def test_raises_past_verified_through(self, cal: EGXCalendar) -> None:
        beyond = cal.verified_through + dt.timedelta(days=1)
        with pytest.raises(CalendarCoverageError, match="verified_through"):
            cal.is_trading_day(beyond)

    def test_non_strict_mode_answers_anyway(self, loose_cal: EGXCalendar) -> None:
        beyond = loose_cal.verified_through + dt.timedelta(days=400)
        assert isinstance(loose_cal.is_trading_day(beyond), bool)

    def test_coverage_does_not_outrun_the_evidence(self, cal: EGXCalendar) -> None:
        """`verified_through` may not exceed the last date the record supports.

        The calendar is derived from the trading record, which trails the present
        by a few sessions. Extending coverage past the evidence reintroduces
        exactly the guessing this file exists to avoid — and a missing holiday
        corrupts settlement dates. Extend it only from EGX's published calendar,
        or by re-running `egx verify-calendar --write` once the feed catches up.
        """
        assert cal.verified_through <= dt.datetime.now(CAIRO).date(), (
            "verified_through claims coverage of dates that have not happened yet"
        )

    def test_islamic_and_coptic_holidays_are_present(self, cal: EGXCalendar) -> None:
        """The whole point of deriving from evidence: these move every year and
        were previously absent, which is what broke settlement math."""
        for day, what in [
            (dt.date(2025, 3, 31), "Eid al-Fitr 2025"),
            (dt.date(2025, 6, 6), "Eid al-Adha 2025"),
            (dt.date(2026, 3, 23), "Eid al-Fitr 2026"),
            (dt.date(2026, 4, 13), "Sham El-Nessim 2026"),
        ]:
            assert not cal.is_trading_day(day), f"{what} should be a non-trading day"

    def test_public_holidays_are_not_assumed_to_be_exchange_holidays(
        self, cal: EGXCalendar
    ) -> None:
        """EGX traded on Police Day and June 30 in 2026 but was closed on both in
        2024. The same nominal holiday differs by year, so the calendar cannot be
        derived from Egypt's public-holiday list."""
        assert cal.is_trading_day(dt.date(2026, 1, 25)) is True
        assert cal.is_trading_day(dt.date(2026, 6, 30)) is True
        assert cal.is_trading_day(dt.date(2024, 1, 25)) is False
        assert cal.is_trading_day(dt.date(2024, 6, 30)) is False


# ── Session phases ───────────────────────────────────────────────────────────


class TestSessionPhase:
    @pytest.mark.parametrize(
        "hh,mm,expected",
        [
            (9, 0, SessionPhase.CLOSED),
            (9, 29, SessionPhase.CLOSED),
            (9, 30, SessionPhase.PRE_OPEN),
            (9, 55, SessionPhase.PRE_OPEN),
            (10, 0, SessionPhase.CONTINUOUS),
            (12, 0, SessionPhase.CONTINUOUS),
            (14, 14, SessionPhase.CONTINUOUS),
            (14, 15, SessionPhase.CLOSING_AUCTION),
            (14, 24, SessionPhase.CLOSING_AUCTION),
            (14, 25, SessionPhase.TRADING_AT_CLOSE),
            (14, 29, SessionPhase.TRADING_AT_CLOSE),
            (14, 30, SessionPhase.CLOSED),
            (18, 0, SessionPhase.CLOSED),
        ],
    )
    def test_phase_boundaries_on_a_trading_day(
        self, cal: EGXCalendar, hh: int, mm: int, expected: SessionPhase
    ) -> None:
        # 2026-08-03 is a Monday.
        assert cal.session_phase(cairo(2026, 8, 3, hh, mm)) is expected

    def test_closed_all_day_on_weekend(self, cal: EGXCalendar) -> None:
        assert cal.session_phase(cairo(2026, 8, 7, 12, 0)) is SessionPhase.CLOSED  # Friday

    def test_closed_all_day_on_holiday(self, cal: EGXCalendar) -> None:
        assert cal.session_phase(cairo(2026, 7, 23, 12, 0)) is SessionPhase.CLOSED

    def test_naive_datetime_is_rejected(self, cal: EGXCalendar) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            cal.session_phase(dt.datetime(2026, 8, 3, 12, 0))  # noqa: DTZ001

    def test_utc_input_is_converted_not_assumed(self, cal: EGXCalendar) -> None:
        """11:00 UTC is 14:00 Cairo — Egypt observes DST, so August is UTC+3."""
        assert cal.session_phase(dt.datetime(2026, 8, 3, 11, 0, tzinfo=UTC)) is (
            SessionPhase.CONTINUOUS
        )

    def test_utc_midday_can_be_after_the_close(self, cal: EGXCalendar) -> None:
        """13:00 UTC is 16:00 Cairo — well after the 14:30 close."""
        assert cal.session_phase(dt.datetime(2026, 8, 3, 13, 0, tzinfo=UTC)) is SessionPhase.CLOSED

    def test_is_open_matches_phase(self, cal: EGXCalendar) -> None:
        assert cal.is_open(cairo(2026, 8, 3, 12, 0)) is True
        assert cal.is_open(cairo(2026, 8, 3, 9, 45)) is True  # pre-open accepts orders
        assert cal.is_open(cairo(2026, 8, 3, 15, 0)) is False

    def test_random_close_window(self, cal: EGXCalendar) -> None:
        assert cal.in_pre_open_random_close(cairo(2026, 8, 3, 9, 45)) is False
        assert cal.in_pre_open_random_close(cairo(2026, 8, 3, 9, 50)) is True
        assert cal.in_pre_open_random_close(cairo(2026, 8, 3, 9, 59)) is True
        # Once continuous trading starts we are out of the discovery window.
        assert cal.in_pre_open_random_close(cairo(2026, 8, 3, 10, 1)) is False


# ── Session arithmetic — this is what settlement depends on ──────────────────


class TestSessionArithmetic:
    def test_next_session_skips_the_weekend(self, cal: EGXCalendar) -> None:
        # Thursday 2026-08-20 -> Sunday 2026-08-23
        assert cal.next_session(dt.date(2026, 8, 6)) == dt.date(2026, 8, 9)

    def test_previous_session_skips_the_weekend(self, cal: EGXCalendar) -> None:
        assert cal.previous_session(dt.date(2026, 8, 9)) == dt.date(2026, 8, 6)

    def test_next_session_skips_a_holiday(self, cal: EGXCalendar) -> None:
        # Tuesday 2026-01-06 -> Wednesday 01-07 is Coptic Christmas -> Thursday 01-08
        assert cal.next_session(dt.date(2026, 1, 6)) == dt.date(2026, 1, 8)

    def test_t_plus_2_within_one_week(self, cal: EGXCalendar) -> None:
        """Sunday buy settles Tuesday."""
        assert cal.add_sessions(dt.date(2026, 8, 2), 2) == dt.date(2026, 8, 4)

    def test_t_plus_2_across_the_weekend(self, cal: EGXCalendar) -> None:
        """Wednesday buy settles Sunday — Fri/Sat do not count."""
        assert cal.add_sessions(dt.date(2026, 8, 5), 2) == dt.date(2026, 8, 9)

    def test_t_plus_2_across_a_holiday(self, cal: EGXCalendar) -> None:
        """Mon 2026-01-05 buy: Tue 01-06 is +1, Wed 01-07 is Coptic Christmas, so
        +2 lands on Thu 01-08."""
        assert cal.add_sessions(dt.date(2026, 1, 5), 2) == dt.date(2026, 1, 8)

    def test_t_plus_2_across_eid_al_fitr(self, cal: EGXCalendar) -> None:
        """The case that motivated deriving the calendar from evidence.

        Eid al-Fitr 2025 closed EGX Sun 03-30 through Wed 04-02. A Thursday
        03-27 buy therefore settles on Mon 04-07, eleven calendar days later.
        The previous hand-written calendar knew nothing of Eid and would have
        reported 03-31 — a date the market was shut, so the system would have
        believed an unsettled lot was sellable."""
        assert cal.add_sessions(dt.date(2025, 3, 27), 2) == dt.date(2025, 4, 7)

    def test_add_zero_sessions_is_identity_on_a_trading_day(self, cal: EGXCalendar) -> None:
        assert cal.add_sessions(dt.date(2026, 8, 3), 0) == dt.date(2026, 8, 3)

    def test_add_zero_sessions_rolls_a_non_trading_day_forward(self, cal: EGXCalendar) -> None:
        """A trade date can never be a non-trading day, but roll rather than lie."""
        assert cal.add_sessions(dt.date(2026, 8, 7), 0) == dt.date(2026, 8, 9)  # Fri -> Sun

    def test_add_sessions_rejects_negative(self, cal: EGXCalendar) -> None:
        with pytest.raises(ValueError, match="does not go backwards"):
            cal.add_sessions(dt.date(2026, 8, 3), -1)

    def test_sessions_between_is_half_open(self, cal: EGXCalendar) -> None:
        # Sun 08-16 .. Wed 08-19 exclusive => Sun, Mon, Tue = 3
        assert cal.sessions_between(dt.date(2026, 8, 2), dt.date(2026, 8, 5)) == 3

    def test_sessions_between_excludes_weekend(self, cal: EGXCalendar) -> None:
        # Thu 08-20 .. Mon 08-24 exclusive => Thu, Sun = 2
        assert cal.sessions_between(dt.date(2026, 8, 6), dt.date(2026, 8, 10)) == 2

    def test_sessions_between_is_zero_when_reversed(self, cal: EGXCalendar) -> None:
        assert cal.sessions_between(dt.date(2026, 8, 5), dt.date(2026, 8, 2)) == 0
        assert cal.sessions_between(dt.date(2026, 8, 2), dt.date(2026, 8, 2)) == 0

    def test_add_sessions_and_sessions_between_agree(self, cal: EGXCalendar) -> None:
        """Round-trip: counting sessions to a settle date returns the settlement lag."""
        # Kept inside verified coverage: settle dates must not run past
        # verified_through, or the strict calendar rightly refuses to answer.
        for start_offset in range(7):
            trade_date = dt.date(2026, 8, 2) + dt.timedelta(days=start_offset)
            if not cal.is_trading_day(trade_date):
                continue
            settle = cal.add_sessions(trade_date, 2)
            assert cal.sessions_between(trade_date, settle) == 2, trade_date

    def test_trading_days_in_range(self, cal: EGXCalendar) -> None:
        days = cal.trading_days_in(dt.date(2026, 8, 2), dt.date(2026, 8, 8))
        assert days == [
            dt.date(2026, 8, 2),
            dt.date(2026, 8, 3),
            dt.date(2026, 8, 4),
            dt.date(2026, 8, 5),
            dt.date(2026, 8, 6),
        ]
