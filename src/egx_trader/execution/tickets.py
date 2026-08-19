"""Order tickets and their lifecycle.

A ticket is a proposed order plus everything a human needs to approve it. It has
one job beyond carrying the order: making it impossible for an order to be placed
twice, or to be placed because nobody answered.

Three rules, each of which exists because the alternative is a real failure:

**Expiry never means execute.** A ticket nobody confirmed is discarded. The
opposite default — "no objection, so proceed" — turns being away from the desk
into a trading decision.

**Idempotency is by key, not by hope.** A ticket carries a deterministic id derived
from what it is. A retry after a timeout, a double-tap on Confirm, a restarted
process replaying its queue — all resolve to the same id, and the second attempt is
a no-op rather than a second position.

**The state machine is explicit and one-way.** There is no path from PLACED back to
PENDING. A retry creates a new ticket; it never rewinds an old one, because
rewinding is how the same order gets submitted twice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

from egx_trader.execution.orders import Order
from egx_trader.market_calendar import CAIRO


class TicketState(StrEnum):
    PENDING = "pending"
    """Waiting on a human."""

    APPROVED = "approved"
    """A human said yes. Not yet sent."""

    PLACED = "placed"
    """Submitted to the broker. Terminal as far as this system is concerned."""

    REJECTED = "rejected"
    """A human said no."""

    EXPIRED = "expired"
    """Nobody answered in time. Explicitly NOT executed."""

    FAILED = "failed"
    """Submission was attempted and failed."""

    @property
    def is_terminal(self) -> bool:
        return self is not TicketState.PENDING and self is not TicketState.APPROVED

    @property
    def resulted_in_an_order(self) -> bool:
        return self is TicketState.PLACED


# Only these transitions exist. Anything else is a bug, and raising beats
# silently accepting a state change that could resubmit an order.
_ALLOWED: dict[TicketState, frozenset[TicketState]] = {
    TicketState.PENDING: frozenset(
        {TicketState.APPROVED, TicketState.REJECTED, TicketState.EXPIRED}
    ),
    TicketState.APPROVED: frozenset({TicketState.PLACED, TicketState.FAILED}),
    TicketState.PLACED: frozenset(),
    TicketState.REJECTED: frozenset(),
    TicketState.EXPIRED: frozenset(),
    TicketState.FAILED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    """Attempted a state change the lifecycle does not permit."""


def ticket_id(order: Order, created_at: dt.datetime) -> str:
    """Deterministic id from the order's identity and the session it belongs to.

    Keyed to the DAY, not the timestamp: two identical tickets minutes apart in the
    same session are the same intent, and giving them different ids would let a
    retry become a second position. A genuinely new order on a later day gets a new
    id naturally.
    """
    parts = "|".join(
        [
            order.symbol,
            order.side.value,
            order.order_type.value,
            str(order.quantity),
            f"{order.limit_price or 0:.4f}",
            f"{order.stop_price or 0:.4f}",
            created_at.astimezone(CAIRO).date().isoformat(),
        ]
    )
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


@dataclass
class Ticket:
    """A proposed order awaiting a decision."""

    order: Order
    created_at: dt.datetime
    expires_at: dt.datetime
    state: TicketState = TicketState.PENDING
    id: str = ""
    rationale: str = ""
    risk_notes: list[str] = field(default_factory=list)
    history: list[tuple[dt.datetime, TicketState, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("ticket timestamps must be timezone-aware")
        if not self.id:
            self.id = ticket_id(self.order, self.created_at)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def transition(self, to: TicketState, when: dt.datetime, note: str = "") -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalTransitionError(
                f"ticket {self.id}: cannot go {self.state.value} -> {to.value}. "
                "Create a new ticket rather than rewinding this one — rewinding is "
                "how the same order gets submitted twice."
            )
        self.history.append((when, to, note))
        self.state = to

    def approve(self, when: dt.datetime, by: str = "human") -> None:
        self.transition(TicketState.APPROVED, when, f"approved by {by}")

    def reject(self, when: dt.datetime, why: str = "") -> None:
        self.transition(TicketState.REJECTED, when, why or "rejected")

    def mark_placed(self, when: dt.datetime, broker_ref: str = "") -> None:
        self.transition(TicketState.PLACED, when, broker_ref or "placed")

    def mark_failed(self, when: dt.datetime, why: str) -> None:
        self.transition(TicketState.FAILED, when, why)

    def expire_if_due(self, now: dt.datetime) -> bool:
        """Expire a pending ticket whose window has passed.

        Returns whether it expired. Note what this does NOT do: it never places the
        order. Silence is not consent.
        """
        if self.state is not TicketState.PENDING or now < self.expires_at:
            return False
        self.transition(TicketState.EXPIRED, now, "no answer before the deadline")
        return True

    @property
    def seconds_remaining(self) -> float:
        return (self.expires_at - dt.datetime.now(CAIRO)).total_seconds()

    # ── presentation ─────────────────────────────────────────────────────────

    def summary(self) -> str:
        """One line, for a notification. Everything needed to decide."""
        order = self.order
        price = order.limit_price or order.stop_price
        priced = f"@ {price:,.2f}" if price else "at market"
        return (
            f"{order.side.value.upper()} {order.quantity:,} {order.symbol} "
            f"{priced} ({order.order_type.value}) "
            f"— {order.notional:,.0f} EGP"
        )


class TicketBook:
    """Live tickets, keyed by id so a duplicate cannot become a second order."""

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}

    def __len__(self) -> int:
        return len(self._tickets)

    def get(self, ticket_id_: str) -> Ticket | None:
        return self._tickets.get(ticket_id_)

    def add(self, ticket: Ticket) -> Ticket:
        """Register a ticket, or return the existing one with the same id.

        Returning the existing ticket rather than raising is deliberate: a caller
        retrying after a timeout should find the original, not an error it then has
        to interpret.
        """
        existing = self._tickets.get(ticket.id)
        if existing is not None:
            return existing
        self._tickets[ticket.id] = ticket
        return ticket

    def pending(self) -> list[Ticket]:
        return [t for t in self._tickets.values() if t.state is TicketState.PENDING]

    def expire_due(self, now: dt.datetime) -> list[Ticket]:
        return [t for t in self.pending() if t.expire_if_due(now)]

    def placed(self) -> list[Ticket]:
        return [t for t in self._tickets.values() if t.state is TicketState.PLACED]
