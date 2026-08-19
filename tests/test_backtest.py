"""Backtest engine tests.

The engine's value is being pessimistic in the specific ways EGX is. Every test
here names the way a backtest lies to itself if that pessimism is missing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from egx_trader.backtest.costs import CostModel
from egx_trader.backtest.engine import BacktestConfig, run_backtest
from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.market_calendar import EGXCalendar
from egx_trader.strategies.base import Intent, Signal

START = dt.date(2026, 2, 1)  # a Sunday, clear of holidays


def series_from(closes: list[float], volumes: list[int] | None = None) -> OHLCVSeries:
    return OHLCVSeries(
        symbol="TEST.CA",
        candles=tuple(
            Candle(
                date=START + dt.timedelta(days=i),
                open=c,
                high=c * 1.02,
                low=c * 0.98,
                close=c,
                volume=volumes[i] if volumes else 1_000_000,
            )
            for i, c in enumerate(closes)
        ),
    )


class ScriptedStrategy:
    """Emits a fixed sequence of signals so engine behaviour can be isolated."""

    name = "scripted"

    def __init__(self, script: dict[int, Signal]) -> None:
        self.script = script

    def prepare(self, series: OHLCVSeries) -> None:
        return None

    def evaluate(
        self, series: OHLCVSeries, i: int, *, in_position: bool, entry_index: int | None
    ) -> Intent:
        return Intent(self.script.get(i, Signal.HOLD), series.candles[i].date, "scripted")


class TestNoLookAhead:
    def test_fills_happen_on_the_next_bar_not_the_signal_bar(self) -> None:
        """A strategy sees bars 0..i; the fill is at the open of i+1. Filling on the
        signal bar is free money that does not exist.

        The gap is kept inside EGX's ±20% band on purpose — a bigger one is
        correctly refused as unfillable, which is a different test.
        """
        closes = [100.0, 100.0, 100.0, 115.0, 115.0, 115.0]
        result = run_backtest(
            series_from(closes),
            ScriptedStrategy({2: Signal.ENTER, 4: Signal.EXIT}),
            config=BacktestConfig(slippage=0.0, settlement_sessions=0),
        )
        assert len(result.trades) == 1
        # Signal on bar 2 (close 100) fills at bar 3's open (115), not at 100.
        assert result.trades[0].entry_price == pytest.approx(115.0)


class TestPriceBand:
    def test_cannot_buy_into_a_limit_up_open(self) -> None:
        """EGX caps a session near ±20%. An order queued behind a limit-up does not
        fill, and pretending otherwise posts imaginary returns on the best days."""
        closes = [100.0, 100.0, 100.0, 130.0, 130.0]
        result = run_backtest(
            series_from(closes),
            ScriptedStrategy({2: Signal.ENTER, 3: Signal.EXIT}),
            config=BacktestConfig(slippage=0.0, settlement_sessions=0),
        )
        assert result.trades == []
        assert any("price band" in s for s in result.skipped)

    def test_a_move_inside_the_band_fills(self) -> None:
        closes = [100.0, 100.0, 100.0, 110.0, 110.0, 110.0]
        result = run_backtest(
            series_from(closes),
            ScriptedStrategy({2: Signal.ENTER, 4: Signal.EXIT}),
            config=BacktestConfig(slippage=0.0, settlement_sessions=0),
        )
        assert len(result.trades) == 1


class TestLiquidity:
    def test_size_is_capped_by_the_days_real_volume(self) -> None:
        """Filling more than a few percent of actual volume is fantasy on a market
        where many names trade under 500k EGP a day."""
        closes = [10.0] * 6
        result = run_backtest(
            series_from(closes, volumes=[1_000] * 6),
            ScriptedStrategy({2: Signal.ENTER, 4: Signal.EXIT}),
            config=BacktestConfig(
                capital_egp=1_000_000, volume_cap=0.05, slippage=0.0, settlement_sessions=0
            ),
        )
        assert len(result.trades) == 1
        assert result.trades[0].quantity == 50, "5% of 1,000 shares"

    def test_an_untradeable_name_is_skipped_not_filled(self) -> None:
        result = run_backtest(
            series_from([10.0] * 6, volumes=[1] * 6),
            ScriptedStrategy({2: Signal.ENTER, 4: Signal.EXIT}),
            config=BacktestConfig(slippage=0.0, settlement_sessions=0),
        )
        assert result.trades == []
        assert any("illiquid" in s for s in result.skipped)


class TestSettlement:
    def test_an_exit_waits_for_t_plus_2(self) -> None:
        """A lot bought Sunday cannot be sold until Tuesday. An exit signal before
        settlement executes later than it looks, at a different price."""
        closes = [100.0] * 3 + [105.0 + i for i in range(12)]
        result = run_backtest(
            series_from(closes),
            ScriptedStrategy({2: Signal.ENTER, 3: Signal.EXIT}),
            config=BacktestConfig(slippage=0.0, settlement_sessions=2),
            calendar=EGXCalendar(strict=False),
        )
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.settlement_delayed_days > 0
        assert trade.exit_date > trade.entry_date


class TestCosts:
    """Explicit dates here, independent of the price-series fixture's START."""

    JAN = dt.date(2026, 1, 15)
    FEB = dt.date(2026, 2, 15)

    def test_the_first_fifty_executions_are_free(self) -> None:
        costs = CostModel()
        assert costs.commission(self.JAN, 10_000) == 0.0

    def test_execution_fifty_one_is_charged(self) -> None:
        """Thndr Trader includes 50 free executions a month, then 2 EGP + 0.1%. A
        strategy firing 80 trades a month is worse than one firing 45."""
        costs = CostModel()
        for _ in range(50):
            costs.commission(self.JAN, 10_000)
        assert costs.commission(self.JAN, 10_000) == pytest.approx(2.0 + 10.0)

    def test_the_allowance_resets_each_month(self) -> None:
        costs = CostModel()
        for _ in range(50):
            costs.commission(self.JAN, 1_000)
        assert costs.commission(self.FEB, 1_000) == 0.0

    def test_overrun_months_are_reported(self) -> None:
        costs = CostModel()
        for _ in range(55):
            costs.commission(self.JAN, 1_000)
        assert costs.overrun_months() == {(2026, 1): 5}

    def test_cgt_applies_to_gains_only(self) -> None:
        costs = CostModel()
        assert costs.tax_on(10_000) == pytest.approx(1_000)
        assert costs.tax_on(-10_000) == 0.0, "a loss does not generate a refund"

    def test_the_subscription_is_charged_per_month(self) -> None:
        costs = CostModel()
        assert costs.subscription_cost(self.JAN, dt.date(2026, 3, 31)) == pytest.approx(735.0)
