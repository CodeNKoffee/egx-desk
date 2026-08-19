"""Execution tests.

These guard the last point before an order reaches a broker, so the emphasis is on
what must be impossible: placing twice, placing without approval, and placing
because nobody answered.
"""

from __future__ import annotations

import datetime as dt

import pytest

from egx_trader.config import ExecutionMode
from egx_trader.execution.adapters import (
    AlertAdapter,
    AssistedAdapter,
    AutoAdapter,
    ExecutionError,
    auto_approve,
    build_adapter,
)
from egx_trader.execution.orders import Order, OrderType, Side
from egx_trader.execution.tickets import (
    IllegalTransitionError,
    Ticket,
    TicketBook,
    TicketState,
    ticket_id,
)
from egx_trader.market_calendar import CAIRO

NOW = dt.datetime(2026, 8, 18, 11, 0, tzinfo=CAIRO)
LATER = NOW + dt.timedelta(minutes=20)


def order(qty: int = 10, price: float = 500.0, symbol: str = "BIOC.CA") -> Order:
    return Order(
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        limit_price=price,
    )


def ticket(**over: object) -> Ticket:
    base: dict[str, object] = {
        "order": order(),
        "created_at": NOW,
        "expires_at": NOW + dt.timedelta(minutes=10),
    }
    return Ticket(**{**base, **over})  # type: ignore[arg-type]


class TestExpiry:
    def test_expiry_never_places_the_order(self) -> None:
        """The opposite default — "no objection, so proceed" — turns being away from
        the desk into a trading decision."""
        t = ticket()
        assert t.expire_if_due(LATER) is True
        assert t.state is TicketState.EXPIRED
        assert not t.state.resulted_in_an_order

    def test_a_ticket_inside_its_window_does_not_expire(self) -> None:
        t = ticket()
        assert t.expire_if_due(NOW + dt.timedelta(minutes=5)) is False
        assert t.state is TicketState.PENDING

    def test_an_expired_ticket_cannot_be_approved(self) -> None:
        t = ticket()
        t.expire_if_due(LATER)
        with pytest.raises(IllegalTransitionError):
            t.approve(LATER)

    def test_an_approved_ticket_is_not_expired_by_the_deadline(self) -> None:
        """Once a human has said yes, a slow submission must not silently cancel it."""
        t = ticket()
        t.approve(NOW)
        assert t.expire_if_due(LATER) is False
        assert t.state is TicketState.APPROVED


class TestIdempotency:
    def test_the_same_order_yields_the_same_id(self) -> None:
        assert ticket_id(order(), NOW) == ticket_id(order(), NOW)

    def test_ids_are_keyed_to_the_day_not_the_second(self) -> None:
        """Two identical tickets minutes apart are the same intent. Different ids
        would let a retry become a second position."""
        assert ticket_id(order(), NOW) == ticket_id(order(), NOW + dt.timedelta(minutes=30))

    def test_a_different_day_is_a_different_order(self) -> None:
        assert ticket_id(order(), NOW) != ticket_id(order(), NOW + dt.timedelta(days=1))

    @pytest.mark.parametrize(
        "changed",
        [order(qty=11), order(price=501.0), order(symbol="AMOC.CA")],
    )
    def test_any_material_change_yields_a_new_id(self, changed: Order) -> None:
        assert ticket_id(changed, NOW) != ticket_id(order(), NOW)

    def test_adding_a_duplicate_returns_the_original(self) -> None:
        """A caller retrying after a timeout should find the original, not an error
        it then has to interpret."""
        book = TicketBook()
        first = book.add(ticket())
        second = book.add(ticket())
        assert first is second
        assert len(book) == 1

    def test_a_duplicate_cannot_become_a_second_order(self) -> None:
        book = TicketBook()
        first = book.add(ticket())
        first.approve(NOW)
        first.mark_placed(NOW)
        again = book.add(ticket())
        assert again.state is TicketState.PLACED, "the retry resolves to the placed ticket"
        assert len(book.placed()) == 1


class TestStateMachine:
    def test_placed_is_terminal(self) -> None:
        """There is no path back to pending. Rewinding is how an order is sent twice."""
        t = ticket()
        t.approve(NOW)
        t.mark_placed(NOW)
        for target in (TicketState.PENDING, TicketState.APPROVED, TicketState.PLACED):
            with pytest.raises(IllegalTransitionError, match="new ticket"):
                t.transition(target, NOW)

    def test_a_rejected_ticket_stays_rejected(self) -> None:
        t = ticket()
        t.reject(NOW, "not now")
        with pytest.raises(IllegalTransitionError):
            t.approve(NOW)

    def test_a_pending_ticket_cannot_jump_straight_to_placed(self) -> None:
        with pytest.raises(IllegalTransitionError):
            ticket().mark_placed(NOW)

    def test_history_records_every_step(self) -> None:
        t = ticket()
        t.approve(NOW, by="operator")
        t.mark_placed(NOW, broker_ref="TH-123")
        assert [state for _, state, _ in t.history] == [
            TicketState.APPROVED,
            TicketState.PLACED,
        ]
        assert "operator" in t.history[0][2]
        assert "TH-123" in t.history[1][2]

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Ticket(
                order=order(),
                created_at=dt.datetime(2026, 8, 18, 11, 0),  # noqa: DTZ001
                expires_at=LATER,
            )


class TestAdapters:
    def test_no_adapter_submits_an_unapproved_ticket(self) -> None:
        """The last check before a broker, so it is verified rather than trusted."""
        for adapter in (AlertAdapter(), AssistedAdapter(), AutoAdapter()):
            with pytest.raises(ExecutionError, match="not approved"):
                adapter.submit(ticket())

    def test_alert_mode_does_not_claim_to_have_placed_anything(self) -> None:
        """Saying otherwise would leave the ledger believing it holds a position it
        does not."""
        t = ticket()
        t.approve(NOW)
        result = AlertAdapter().submit(t)
        assert not result.placed
        assert t.state is TicketState.APPROVED
        assert "yourself" in result.message

    def test_assisted_fails_loudly_rather_than_guessing_selectors(self) -> None:
        t = ticket()
        t.approve(NOW)
        with pytest.raises(NotImplementedError, match="logged-in session"):
            AssistedAdapter().submit(t)

    def test_auto_without_a_broker_path_refuses(self) -> None:
        t = ticket()
        t.approve(NOW)
        with pytest.raises(NotImplementedError):
            AutoAdapter().submit(t)

    def test_build_adapter_maps_each_mode(self) -> None:
        assert build_adapter(ExecutionMode.ALERT).mode is ExecutionMode.ALERT
        assert build_adapter(ExecutionMode.ASSISTED).mode is ExecutionMode.ASSISTED
        assert build_adapter(ExecutionMode.AUTO).mode is ExecutionMode.AUTO


class TestAutoApproval:
    def test_an_order_inside_the_cap_is_approved(self) -> None:
        t = ticket()
        assert auto_approve(t, max_order_egp=10_000) is True
        assert t.state is TicketState.APPROVED

    def test_an_over_cap_order_is_refused_not_shrunk(self) -> None:
        """Shrinking an order until it passes means having no cap at all."""
        t = ticket(order=order(qty=100))
        assert auto_approve(t, max_order_egp=10_000) is False
        assert t.state is TicketState.PENDING


class TestBook:
    def test_expire_due_only_touches_pending_tickets(self) -> None:
        book = TicketBook()
        pending = book.add(ticket())
        approved = book.add(ticket(order=order(qty=20)))
        approved.approve(NOW)

        expired = book.expire_due(LATER)
        assert expired == [pending]
        assert approved.state is TicketState.APPROVED
