"""Ledger tests.

Settlement is the point of tracking lots rather than net positions: a system that
gets it wrong tries to sell shares it does not yet have.
"""

from __future__ import annotations

import datetime as dt

import pytest

from egx_trader.portfolio.ledger import InsufficientSharesError, Ledger

SUN = dt.date(2026, 2, 1)
MON = dt.date(2026, 2, 2)
TUE = dt.date(2026, 2, 3)
WED = dt.date(2026, 2, 4)


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(cash_egp=100_000)


class TestSettlement:
    def test_a_fresh_buy_is_not_sellable_the_same_day(self, ledger: Ledger) -> None:
        ledger.buy("BIOC.CA", 100, 500.0, SUN)
        assert ledger.quantity("BIOC.CA") == 100
        assert ledger.sellable_quantity("BIOC.CA", SUN) == 0
        assert ledger.unsettled_quantity("BIOC.CA", SUN) == 100

    def test_settles_after_two_sessions(self, ledger: Ledger) -> None:
        """Sunday buy settles Tuesday."""
        ledger.buy("BIOC.CA", 100, 500.0, SUN)
        assert ledger.sellable_quantity("BIOC.CA", MON) == 0
        assert ledger.sellable_quantity("BIOC.CA", TUE) == 100

    def test_selling_unsettled_shares_raises(self, ledger: Ledger) -> None:
        """Raises rather than partially filling: a silent short-fill leaves the
        ledger and the broker disagreeing."""
        ledger.buy("BIOC.CA", 100, 500.0, SUN)
        with pytest.raises(InsufficientSharesError, match="unsettled"):
            ledger.sell("BIOC.CA", 100, 520.0, MON)

    def test_only_the_settled_portion_is_sellable(self, ledger: Ledger) -> None:
        ledger.buy("BIOC.CA", 100, 500.0, SUN)  # settles Tue
        ledger.buy("BIOC.CA", 50, 510.0, TUE)  # settles Thu
        assert ledger.quantity("BIOC.CA") == 150
        assert ledger.sellable_quantity("BIOC.CA", TUE) == 100

    def test_pre_existing_lots_are_settled(self, ledger: Ledger) -> None:
        """They plainly are — but the flag records that the date is assumed."""
        lot = ledger.add_pre_existing("BIOC.CA", 100, 400.00)
        assert lot.pre_existing
        assert ledger.sellable_quantity("BIOC.CA", SUN) == 100

    def test_a_pre_existing_lot_does_not_move_cash(self, ledger: Ledger) -> None:
        before = ledger.cash_egp
        ledger.add_pre_existing("BIOC.CA", 100, 400.00)
        assert ledger.cash_egp == before


class TestFIFO:
    def test_sells_the_oldest_lot_first(self, ledger: Ledger) -> None:
        """Which shares left decides the realised gain, and therefore the tax."""
        ledger.add_pre_existing("BIOC.CA", 100, 400.0)
        ledger.add_pre_existing("BIOC.CA", 100, 500.0)
        realised = ledger.sell("BIOC.CA", 100, 600.0, SUN)
        assert len(realised) == 1
        assert realised[0].entry_price == 400.0
        assert ledger.average_cost("BIOC.CA") == 500.0

    def test_a_sell_can_span_lots(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("X.CA", 60, 10.0)
        ledger.add_pre_existing("X.CA", 60, 20.0)
        realised = ledger.sell("X.CA", 100, 30.0, SUN)
        assert [(r.quantity, r.entry_price) for r in realised] == [(60, 10.0), (40, 20.0)]

    def test_emptied_lots_are_dropped(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("X.CA", 50, 10.0)
        ledger.sell("X.CA", 50, 12.0, SUN)
        assert ledger.quantity("X.CA") == 0
        assert "X.CA" not in ledger.symbols()


class TestValuation:
    def test_average_cost(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("X.CA", 100, 10.0)
        ledger.add_pre_existing("X.CA", 100, 20.0)
        assert ledger.average_cost("X.CA") == 15.0

    def test_average_cost_of_nothing_is_none(self, ledger: Ledger) -> None:
        assert ledger.average_cost("NOPE.CA") is None

    def test_unrealised_pnl(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("BIOC.CA", 100, 400.0)
        assert ledger.unrealised_pnl({"BIOC.CA": 500.0}) == pytest.approx(10_000)

    def test_equity_is_cash_plus_market_value(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("BIOC.CA", 100, 400.0)
        assert ledger.equity({"BIOC.CA": 500.0}) == pytest.approx(150_000)

    def test_a_missing_price_is_skipped_not_guessed(self, ledger: Ledger) -> None:
        """Valuing an unpriced holding at cost would quietly overstate equity."""
        ledger.add_pre_existing("BIOC.CA", 100, 400.0)
        assert ledger.market_value({}) == 0.0

    def test_concentration(self, ledger: Ledger) -> None:
        ledger.cash_egp = 0
        ledger.add_pre_existing("BIOC.CA", 100, 400.0)
        ledger.add_pre_existing("AMOC.CA", 100, 100.0)
        pct = ledger.concentration({"BIOC.CA": 400.0, "AMOC.CA": 100.0})
        assert pct["BIOC.CA"] == pytest.approx(80.0)


class TestTax:
    def test_reserve_is_ten_percent_of_realised_gains(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("X.CA", 100, 10.0)
        ledger.sell("X.CA", 100, 20.0, SUN)
        assert ledger.realised_pnl() == pytest.approx(1_000)
        assert ledger.tax_reserve() == pytest.approx(100)

    def test_a_realised_loss_owes_nothing(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("X.CA", 100, 20.0)
        ledger.sell("X.CA", 100, 10.0, SUN)
        assert ledger.realised_pnl() < 0
        assert ledger.tax_reserve() == 0.0


class TestCash:
    def test_a_buy_debits_cash(self, ledger: Ledger) -> None:
        ledger.buy("X.CA", 100, 10.0, SUN)
        assert ledger.cash_egp == pytest.approx(99_000)

    def test_a_sell_credits_cash(self, ledger: Ledger) -> None:
        ledger.add_pre_existing("X.CA", 100, 10.0)
        ledger.sell("X.CA", 100, 12.0, SUN)
        assert ledger.cash_egp == pytest.approx(101_200)
