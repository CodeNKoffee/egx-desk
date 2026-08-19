"""Quality gate tests.

The headline case is the real one: BIOC's saved scan shows a price of 345.25
against an MA20 of 134. These gates exist so that never reaches a strategy again.
"""

from __future__ import annotations

import datetime as dt

import pytest

from egx_trader.data.models import Candle, OHLCVSeries, Split
from egx_trader.data.quality import (
    IssueCode,
    QualityThresholds,
    Severity,
    check_series,
)
from egx_trader.market_calendar import EGXCalendar

START = dt.date(2026, 1, 4)  # a Sunday


def bar(day_offset: int, close: float, *, volume: int = 1_000) -> Candle:
    return Candle(
        date=START + dt.timedelta(days=day_offset),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=volume,
    )


def series(closes: list[float], *, splits: tuple[Split, ...] = (), **kwargs: object) -> OHLCVSeries:
    return OHLCVSeries(
        symbol="TEST.CA",
        candles=tuple(bar(i, c) for i, c in enumerate(closes)),
        splits=splits,
        **kwargs,  # type: ignore[arg-type]
    )


def steady(n: int = 80, start: float = 100.0) -> list[float]:
    """A gently drifting price path that trips no gates."""
    return [start * (1.002**i) for i in range(n)]


class TestCleanSeries:
    def test_a_healthy_series_is_usable(self) -> None:
        report = check_series(series(steady()))
        assert report.is_usable
        assert not report.issues

    def test_empty_series_is_an_error(self) -> None:
        report = check_series(OHLCVSeries(symbol="TEST.CA", candles=()))
        assert not report.is_usable
        assert report.has(IssueCode.NO_DATA)

    def test_short_history_is_an_error(self) -> None:
        report = check_series(series(steady(30)))
        assert not report.is_usable
        assert report.has(IssueCode.INSUFFICIENT_HISTORY)


class TestPriceJumps:
    def test_unexplained_jump_beyond_egx_limits_is_an_error(self) -> None:
        """A 150% step in one session is impossible under a ±20% band."""
        closes = steady(80)
        closes[60] = closes[59] * 2.5
        report = check_series(series(closes))

        assert not report.is_usable
        assert report.has(IssueCode.UNEXPLAINED_PRICE_JUMP)
        assert "price band" in report.errors[0].message

    def test_a_reported_split_explains_the_jump(self) -> None:
        closes = steady(80)
        jump_date = START + dt.timedelta(days=60)
        closes[60] = closes[59] * 2.0
        report = check_series(
            series(
                closes,
                splits=(Split(date=jump_date, numerator=2.0, denominator=1.0),),
            )
        )
        assert report.is_usable
        assert not report.has(IssueCode.UNEXPLAINED_PRICE_JUMP)

    def test_split_reported_a_few_days_off_still_explains(self) -> None:
        """Vendor and exchange split dates disagree routinely."""
        closes = steady(80)
        closes[60] = closes[59] * 2.0
        report = check_series(
            series(
                closes,
                splits=(
                    Split(
                        date=START + dt.timedelta(days=62),
                        numerator=2.0,
                        denominator=1.0,
                    ),
                ),
            )
        )
        assert report.is_usable

    def test_a_split_far_away_does_not_excuse_the_jump(self) -> None:
        closes = steady(80)
        closes[60] = closes[59] * 2.0
        report = check_series(
            series(
                closes,
                splits=(Split(date=START + dt.timedelta(days=5), numerator=2.0, denominator=1.0),),
            )
        )
        assert not report.is_usable

    def test_a_split_of_the_wrong_size_does_not_excuse_the_jump(self) -> None:
        closes = steady(80)
        closes[60] = closes[59] * 5.0
        report = check_series(
            series(
                closes,
                splits=(
                    Split(
                        date=START + dt.timedelta(days=60),
                        numerator=2.0,
                        denominator=1.0,
                    ),
                ),
            )
        )
        assert not report.is_usable

    @pytest.mark.parametrize("move", [0.19, -0.19, 0.199, -0.199])
    def test_moves_within_egx_limits_are_allowed(self, move: float) -> None:
        """A genuine limit-up day must not be flagged as corrupt."""
        closes = steady(80)
        closes[60] = closes[59] * (1 + move)
        closes[61:] = [closes[60] * (1.002**i) for i in range(1, len(closes) - 60)]
        report = check_series(series(closes))
        assert not report.has(IssueCode.UNEXPLAINED_PRICE_JUMP)

    def test_a_multi_session_gap_widens_the_allowance(self) -> None:
        """Yahoo skips sessions, so one bar step can span several days of limit-ups.

        This is BIOC's 2026-08-04 bar: +148.8% across four sessions, which four
        consecutive +20% days fully account for.
        """
        cal = EGXCalendar(strict=False)
        # Sun 2026-02-01 .. Thu 2026-02-05, with the sessions between missing.
        # (A January window would have straddled Coptic Christmas and left only
        # three sessions — the holiday calendar changes the answer here.)
        candles = (
            Candle(date=dt.date(2026, 2, 1), open=100, high=101, low=99, close=100, volume=1_000),
            Candle(date=dt.date(2026, 2, 5), open=200, high=210, low=195, close=200, volume=9_000),
        )
        data = OHLCVSeries(symbol="TEST.CA", candles=candles)
        thresholds = QualityThresholds(min_bars=2)

        blind = check_series(data, thresholds)
        assert blind.has(IssueCode.UNEXPLAINED_PRICE_JUMP), "one-session reading rejects it"

        aware = check_series(data, thresholds, calendar=cal)
        assert not aware.has(IssueCode.UNEXPLAINED_PRICE_JUMP), (
            "four sessions of +20% permit a doubling"
        )

    def test_a_gap_still_cannot_excuse_anything(self) -> None:
        """The allowance compounds, it does not become unlimited."""
        cal = EGXCalendar(strict=False)
        candles = (
            Candle(date=dt.date(2026, 2, 1), open=100, high=101, low=99, close=100, volume=1_000),
            Candle(date=dt.date(2026, 2, 5), open=900, high=910, low=890, close=900, volume=9_000),
        )
        report = check_series(
            OHLCVSeries(symbol="TEST.CA", candles=candles),
            QualityThresholds(min_bars=2),
            calendar=cal,
        )
        assert report.has(IssueCode.UNEXPLAINED_PRICE_JUMP)

    def test_a_reverse_split_is_recognised(self) -> None:
        closes = steady(80)
        closes[60] = closes[59] / 4.0
        closes[61:] = [closes[60] * (1.002**i) for i in range(1, len(closes) - 60)]
        report = check_series(
            series(
                closes,
                splits=(
                    Split(
                        date=START + dt.timedelta(days=60),
                        numerator=1.0,
                        denominator=4.0,
                    ),
                ),
            )
        )
        assert report.is_usable


class TestDropRate:
    def test_high_drop_rate_is_an_error(self) -> None:
        report = check_series(series(steady()), dropped_fraction=0.4)
        assert not report.is_usable
        assert report.has(IssueCode.HIGH_DROP_RATE)

    def test_moderate_drop_rate_is_only_a_warning(self) -> None:
        report = check_series(series(steady()), dropped_fraction=0.1)
        assert report.is_usable
        assert report.has(IssueCode.HIGH_DROP_RATE)
        assert report.warnings

    def test_low_drop_rate_is_silent(self) -> None:
        assert not check_series(series(steady()), dropped_fraction=0.01).issues


class TestFeedMetadata:
    def test_mutualfund_type_warns_but_does_not_block(self) -> None:
        """Yahoo types EGX equities MUTUALFUND. Worth flagging, not worth halting."""
        report = check_series(series(steady(), instrument_type="MUTUALFUND"))
        assert report.is_usable
        assert report.has(IssueCode.UNEXPECTED_INSTRUMENT_TYPE)

    def test_equity_type_is_silent(self) -> None:
        assert not check_series(series(steady(), instrument_type="EQUITY")).issues

    def test_stale_exchange_timestamp_warns(self) -> None:
        report = check_series(
            series(
                steady(),
                exchange_timestamp=dt.datetime(2024, 7, 23, tzinfo=dt.UTC),
            )
        )
        assert report.is_usable
        assert report.has(IssueCode.STALE_EXCHANGE_TIME)

    def test_stale_last_bar_is_an_error(self) -> None:
        """A series that stops weeks ago must not be traded on."""
        report = check_series(series(steady()), as_of=START + dt.timedelta(days=200))
        assert not report.is_usable
        assert report.has(IssueCode.STALE_LAST_BAR)

    def test_current_last_bar_is_fine(self) -> None:
        data = series(steady())
        assert data.last_date is not None
        report = check_series(data, as_of=data.last_date + dt.timedelta(days=1))
        assert not report.has(IssueCode.STALE_LAST_BAR)


class TestLiquidityAndDeadFeeds:
    def test_mostly_zero_volume_warns(self) -> None:
        candles = tuple(bar(i, c, volume=0 if i % 2 == 0 else 1000) for i, c in enumerate(steady()))
        report = check_series(OHLCVSeries(symbol="TEST.CA", candles=candles))
        assert report.has(IssueCode.ZERO_VOLUME_RUN)
        assert report.is_usable

    def test_flatlined_price_warns(self) -> None:
        closes = steady(80)
        closes[40:55] = [closes[39]] * 15
        report = check_series(series(closes))
        assert report.has(IssueCode.FLATLINED_PRICE)
        assert report.is_usable

    def test_flatline_only_fires_once(self) -> None:
        closes = [100.0] * 80
        report = check_series(series(closes))
        assert sum(1 for i in report.issues if i.code is IssueCode.FLATLINED_PRICE) == 1


class TestThresholdsAreTunable:
    def test_stricter_move_limit_catches_smaller_jumps(self) -> None:
        closes = steady(80)
        closes[60] = closes[59] * 1.15
        closes[61:] = [closes[60] * (1.002**i) for i in range(1, len(closes) - 60)]

        assert check_series(series(closes)).is_usable
        strict = check_series(series(closes), QualityThresholds(max_daily_move_pct=10.0))
        assert not strict.is_usable


class TestReporting:
    def test_summary_counts_severities(self) -> None:
        report = check_series(series(steady(), instrument_type="MUTUALFUND"))
        assert "0 error(s), 1 warning(s)" in report.summary()

    def test_clean_summary(self) -> None:
        assert check_series(series(steady())).summary().endswith("clean")

    def test_issue_renders_readably(self) -> None:
        report = check_series(series(steady(30)))
        rendered = str(report.issues[0])
        assert rendered.startswith(Severity.ERROR.value.upper())
        assert IssueCode.INSUFFICIENT_HISTORY.value in rendered
