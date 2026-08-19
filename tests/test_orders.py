"""Order routing tests.

The rules encode EGX microstructure, not preference: price limits and halts, thin
books, T+0 requiring a price, and stops firing at market. Each test names the
market fact it protects.
"""

from __future__ import annotations

import pytest

from egx_trader.execution.orders import (
    Order,
    OrderType,
    RoutingPolicy,
    Settlement,
    Side,
    Urgency,
    choose_order_type,
    protective_stop,
)

LIQUID = 5_000_000.0
THIN = 50_000.0


class TestOrderValidation:
    def test_limit_order_needs_a_price(self) -> None:
        with pytest.raises(ValueError, match="needs a limit price"):
            Order(symbol="BIOC.CA", side=Side.BUY, order_type=OrderType.LIMIT, quantity=10)

    def test_stop_order_needs_a_stop_price(self) -> None:
        with pytest.raises(ValueError, match="needs a stop price"):
            Order(symbol="BIOC.CA", side=Side.SELL, order_type=OrderType.STOP, quantity=10)

    def test_market_order_cannot_carry_a_limit(self) -> None:
        with pytest.raises(ValueError, match="pick one"):
            Order(
                symbol="BIOC.CA",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=10,
                limit_price=500.0,
            )

    def test_t0_cannot_be_a_market_order(self) -> None:
        """Thndr's advanced order screen requires a price. This is a platform fact,
        not a preference — a same-session exit is a limit order or it is nothing."""
        with pytest.raises(ValueError, match="advanced limit screen"):
            Order(
                symbol="BIOC.CA",
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=10,
                settlement=Settlement.T0,
            )

    def test_t0_limit_order_is_accepted(self) -> None:
        order = Order(
            symbol="BIOC.CA",
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=10,
            limit_price=500.0,
            settlement=Settlement.T0,
        )
        assert order.notional == 5000.0

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            Order(symbol="X.CA", side=Side.BUY, order_type=OrderType.MARKET, quantity=0)


class TestEntryRouting:
    def test_entries_are_never_market_orders(self) -> None:
        """A breakout is by definition a moment the book is moving, which is exactly
        when a market order fills worst."""
        for turnover in (THIN, LIQUID, None):
            decision = choose_order_type(
                side=Side.BUY,
                urgency=Urgency.NORMAL,
                last_price=100.0,
                avg_daily_turnover_egp=turnover,
            )
            assert decision.order_type is OrderType.LIMIT

    def test_entry_is_priced_slightly_through_last(self) -> None:
        decision = choose_order_type(side=Side.BUY, urgency=Urgency.NORMAL, last_price=100.0)
        assert decision.limit_price == 100.5

    def test_entry_offset_does_not_chase_into_a_halt(self) -> None:
        """EGX halts a stock on a ±10% MVWAP move. An entry must stay far inside that."""
        decision = choose_order_type(side=Side.BUY, urgency=Urgency.URGENT, last_price=100.0)
        assert decision.limit_price is not None
        assert (decision.limit_price / 100.0 - 1) * 100 < 5.0


class TestExitRouting:
    def test_urgent_exit_on_a_liquid_name_uses_market(self) -> None:
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.URGENT,
            last_price=100.0,
            avg_daily_turnover_egp=LIQUID,
        )
        assert decision.order_type is OrderType.MARKET
        assert decision.limit_price is None

    def test_urgent_exit_on_a_thin_name_stays_a_limit(self) -> None:
        """A market order walks the book, and on a name trading 50k EGP a day it
        walks it a long way."""
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.URGENT,
            last_price=100.0,
            avg_daily_turnover_egp=THIN,
        )
        assert decision.order_type is OrderType.LIMIT
        assert decision.limit_price == 97.0

    def test_unknown_liquidity_is_treated_as_thin(self) -> None:
        """Unknown must never read as permissive — the same rule as T+0 eligibility."""
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.URGENT,
            last_price=100.0,
            avg_daily_turnover_egp=None,
        )
        assert decision.order_type is OrderType.LIMIT

    def test_an_urgent_sell_limit_sits_below_last_so_it_still_fills(self) -> None:
        """A sell limit under the market executes immediately; it only caps how far
        down. That buys most of a market order's certainty without its worst fill."""
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.URGENT,
            last_price=100.0,
            avg_daily_turnover_egp=THIN,
        )
        assert decision.limit_price is not None
        assert decision.limit_price < 100.0

    def test_ordinary_exit_does_not_pay_up(self) -> None:
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.NORMAL,
            last_price=100.0,
            avg_daily_turnover_egp=LIQUID,
        )
        assert decision.order_type is OrderType.LIMIT
        assert decision.limit_price == 100.0


class TestT0Routing:
    def test_t0_always_routes_as_a_limit(self) -> None:
        for urgency in Urgency:
            decision = choose_order_type(
                side=Side.SELL,
                urgency=urgency,
                last_price=100.0,
                avg_daily_turnover_egp=LIQUID,
                settlement=Settlement.T0,
            )
            assert decision.order_type is OrderType.LIMIT, urgency
            assert "advanced limit screen" in decision.rationale

    def test_t0_beats_the_liquidity_rule(self) -> None:
        """Even the most liquid name cannot use a market order for a same-session
        exit — the platform will not accept one."""
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.URGENT,
            last_price=100.0,
            avg_daily_turnover_egp=LIQUID * 100,
            settlement=Settlement.T0,
        )
        assert decision.order_type is OrderType.LIMIT


class TestProtectiveStop:
    def test_builds_a_resting_broker_stop(self) -> None:
        order = protective_stop(symbol="BIOC.CA", quantity=128, stop_price=441.03)
        assert order.order_type is OrderType.STOP
        assert order.side is Side.SELL
        assert order.stop_price == 441.03
        assert order.limit_price is None

    def test_the_rationale_says_who_enforces_it(self) -> None:
        """The point of a resting stop is that a crashed bot cannot leave a position
        unprotected."""
        assert "broker" in protective_stop(symbol="X.CA", quantity=1, stop_price=10.0).reason


class TestPolicyIsTunable:
    def test_a_wider_entry_offset_applies(self) -> None:
        decision = choose_order_type(
            side=Side.BUY,
            urgency=Urgency.NORMAL,
            last_price=100.0,
            policy=RoutingPolicy(entry_limit_offset_pct=2.0),
        )
        assert decision.limit_price == 102.0

    def test_a_higher_liquidity_bar_downgrades_market_to_limit(self) -> None:
        policy = RoutingPolicy(min_daily_turnover_egp=LIQUID * 10)
        decision = choose_order_type(
            side=Side.SELL,
            urgency=Urgency.URGENT,
            last_price=100.0,
            avg_daily_turnover_egp=LIQUID,
            policy=policy,
        )
        assert decision.order_type is OrderType.LIMIT
