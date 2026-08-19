"""Risk gate tests.

Two gates carry most of the value: settlement, because the broker rejects what it
blocks, and the order budget, because a naive version can block you out of selling.
"""

from __future__ import annotations

import datetime as dt

import pytest

from egx_trader.execution.orders import Order, OrderType, Side
from egx_trader.portfolio.ledger import Ledger
from egx_trader.risk.engine import Gate, RiskContext, RiskEngine, RiskLimits
from egx_trader.universe import Instrument, InstrumentStatus

SUN = dt.date(2026, 2, 1)
MON = dt.date(2026, 2, 2)
TUE = dt.date(2026, 2, 3)
PRICES = {"BIOC.CA": 500.0, "AMOC.CA": 10.0}


def buy(symbol: str = "BIOC.CA", qty: int = 10, price: float = 500.0) -> Order:
    return Order(
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        limit_price=price,
    )


def sell(symbol: str = "BIOC.CA", qty: int = 10, price: float = 500.0) -> Order:
    return Order(
        symbol=symbol,
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        quantity=qty,
        limit_price=price,
    )


def ctx(**over: object) -> RiskContext:
    base: dict[str, object] = {"when": TUE, "prices": PRICES}
    return RiskContext(**{**base, **over})  # type: ignore[arg-type]


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(RiskLimits(max_order_egp=100_000, max_position_pct=90.0))


class TestSettlementGate:
    def test_selling_unsettled_shares_is_blocked(self, engine: RiskEngine) -> None:
        ledger = Ledger(cash_egp=100_000)
        ledger.buy("BIOC.CA", 100, 500.0, SUN)  # settles Tuesday
        decision = engine.check(sell(qty=100), ledger, ctx(when=MON))
        assert not decision.allowed
        assert Gate.SETTLEMENT in decision.blocked_by
        assert "unsettled" in decision.explain()

    def test_selling_settled_shares_is_allowed(self, engine: RiskEngine) -> None:
        ledger = Ledger(cash_egp=100_000)
        ledger.buy("BIOC.CA", 100, 500.0, SUN)
        assert engine.check(sell(qty=100), ledger, ctx(when=TUE)).allowed


class TestOrderBudget:
    def test_entries_stop_early_to_leave_room_for_exits(self) -> None:
        """The whole point: running out of executions must never stop you selling."""
        engine = RiskEngine(RiskLimits(free_trades_per_month=50, exit_reserve=10))
        ledger = Ledger(cash_egp=1_000_000)
        decision = engine.check(buy(), ledger, ctx(executions_this_month=40))
        assert not decision.allowed
        assert Gate.ORDER_BUDGET in decision.blocked_by

    def test_exits_are_never_blocked_by_the_budget(self) -> None:
        engine = RiskEngine(RiskLimits(free_trades_per_month=50, exit_reserve=10))
        ledger = Ledger(cash_egp=0)
        ledger.add_pre_existing("BIOC.CA", 100, 400.0)
        decision = engine.check(sell(qty=100), ledger, ctx(executions_this_month=500))
        assert decision.allowed, "an exit must survive any allowance state"

    def test_entries_are_allowed_below_the_budget(self) -> None:
        engine = RiskEngine(RiskLimits(free_trades_per_month=50, exit_reserve=10))
        ledger = Ledger(cash_egp=1_000_000)
        assert engine.check(buy(), ledger, ctx(executions_this_month=10)).allowed


class TestConcentration:
    def test_measured_on_the_resulting_position(self) -> None:
        engine = RiskEngine(RiskLimits(max_position_pct=25.0, max_order_egp=1_000_000))
        ledger = Ledger(cash_egp=100_000)
        decision = engine.check(buy(qty=100), ledger, ctx())
        assert not decision.allowed
        assert Gate.POSITION_CONCENTRATION in decision.blocked_by

    def test_a_small_addition_is_allowed(self) -> None:
        engine = RiskEngine(RiskLimits(max_position_pct=25.0, max_order_egp=1_000_000))
        ledger = Ledger(cash_egp=1_000_000)
        assert engine.check(buy(qty=10), ledger, ctx()).allowed

    def test_grandfathering_does_not_licence_adding_to_an_overweight_name(self) -> None:
        """A holding that predates the bot. Not forcing a trim is one thing; letting the bot
        buy more is another."""
        engine = RiskEngine(
            RiskLimits(max_position_pct=25.0, max_order_egp=1_000_000, grandfather_existing=True)
        )
        ledger = Ledger(cash_egp=10_000)
        ledger.add_pre_existing("BIOC.CA", 100, 400.00)
        decision = engine.check(buy(qty=10), ledger, ctx())
        assert not decision.allowed
        assert Gate.POSITION_CONCENTRATION in decision.blocked_by


class TestOtherGates:
    def test_kill_switch_blocks_everything(self, engine: RiskEngine) -> None:
        ledger = Ledger(cash_egp=1_000_000)
        decision = engine.check(buy(), ledger, ctx(kill_switch=True))
        assert Gate.KILL_SWITCH in decision.blocked_by

    def test_the_daily_loss_limit_trips(self) -> None:
        engine = RiskEngine(RiskLimits(daily_loss_limit_pct=4.0, max_order_egp=1_000_000))
        ledger = Ledger(cash_egp=95_000)
        decision = engine.check(buy(), ledger, ctx(day_start_equity=100_000))
        assert Gate.DAILY_LOSS in decision.blocked_by

    def test_the_order_cap_applies(self) -> None:
        engine = RiskEngine(RiskLimits(max_order_egp=1_000))
        decision = engine.check(buy(qty=100), Ledger(cash_egp=1_000_000), ctx())
        assert Gate.ORDER_SIZE in decision.blocked_by

    def test_insufficient_cash_blocks(self, engine: RiskEngine) -> None:
        decision = engine.check(buy(qty=10), Ledger(cash_egp=100), ctx())
        assert Gate.CASH in decision.blocked_by
        assert "unsettled proceeds" in decision.explain()

    def test_an_untradable_instrument_blocks(self, engine: RiskEngine) -> None:
        paused = Instrument(symbol="EMDE.CA", name_en="x", status=InstrumentStatus.PAUSED)
        decision = engine.check(
            buy(symbol="EMDE.CA"),
            Ledger(cash_egp=1_000_000),
            ctx(instrument=paused, prices={"EMDE.CA": 10.0}),
        )
        assert Gate.UNIVERSE in decision.blocked_by

    def test_liquidity_caps_the_order(self, engine: RiskEngine) -> None:
        decision = engine.check(
            buy(qty=1_000), Ledger(cash_egp=10_000_000), ctx(avg_daily_volume=1_000)
        )
        assert Gate.LIQUIDITY in decision.blocked_by

    def test_the_new_position_limit_applies(self) -> None:
        engine = RiskEngine(RiskLimits(max_new_positions_per_day=3, max_order_egp=1_000_000))
        decision = engine.check(buy(), Ledger(cash_egp=1_000_000), ctx(new_positions_today=3))
        assert Gate.NEW_POSITIONS in decision.blocked_by


class TestReporting:
    def test_every_failure_is_reported_not_just_the_first(self) -> None:
        """Reporting one at a time means fixing one and rediscovering the next."""
        engine = RiskEngine(RiskLimits(max_order_egp=100))
        decision = engine.check(buy(qty=1_000), Ledger(cash_egp=10), ctx(kill_switch=True))
        assert len(decision.blocked_by) >= 3
        assert Gate.KILL_SWITCH in decision.blocked_by
        assert Gate.ORDER_SIZE in decision.blocked_by
        assert Gate.CASH in decision.blocked_by

    def test_an_allowed_order_explains_itself(self, engine: RiskEngine) -> None:
        assert engine.check(buy(), Ledger(cash_egp=1_000_000), ctx()).explain() == "allowed"


class TestExitsAreNeverBlocked:
    """The overriding rule. A gate that blocks a sell creates risk rather than
    reducing it — you would hold a position the software refuses to release.
    Downside protection comes from broker-resting stops, which work whether or not
    this process is running."""

    def _held(self) -> Ledger:
        ledger = Ledger(cash_egp=0)
        ledger.add_pre_existing("BIOC.CA", 100, 400.00)
        return ledger

    def test_an_exit_larger_than_the_order_cap_is_allowed(self) -> None:
        """Found by a failing test: a 64,000 EGP position under a 10,000 cap would
        have been impossible to liquidate in one order."""
        engine = RiskEngine(RiskLimits(max_order_egp=10_000))
        assert engine.check(sell(qty=100), self._held(), ctx()).allowed

    def test_an_exit_survives_the_kill_switch(self) -> None:
        """Stopping new risk is not the same as trapping existing risk."""
        engine = RiskEngine(RiskLimits())
        assert engine.check(sell(qty=100), self._held(), ctx(kill_switch=True)).allowed

    def test_an_exit_survives_the_daily_loss_limit(self) -> None:
        """Being down 4% is precisely when selling matters most."""
        engine = RiskEngine(RiskLimits(daily_loss_limit_pct=4.0))
        decision = engine.check(sell(qty=100), self._held(), ctx(day_start_equity=1_000_000))
        assert decision.allowed

    def test_an_exit_survives_an_exhausted_execution_budget(self) -> None:
        engine = RiskEngine(RiskLimits(free_trades_per_month=50))
        assert engine.check(sell(qty=100), self._held(), ctx(executions_this_month=999)).allowed

    def test_but_an_exit_still_cannot_sell_unsettled_shares(self) -> None:
        """The one exception, and it is the broker's rule rather than ours."""
        engine = RiskEngine(RiskLimits())
        ledger = Ledger(cash_egp=100_000)
        ledger.buy("BIOC.CA", 100, 500.0, SUN)
        decision = engine.check(sell(qty=100), ledger, ctx(when=MON))
        assert not decision.allowed
        assert decision.blocked_by == [Gate.SETTLEMENT]
