"""Execution adapters: alert, assisted, auto.

The same order flows through all three. What differs is only who presses confirm,
and each adapter is a thin layer over the same ticket lifecycle so the safety
machinery cannot be bypassed by picking a different mode.

`alert` never touches a browser at all. `assisted` fills the ThndrX ticket and
stops. `auto` confirms itself, and is double-gated in config so it cannot be
reached by one typo.

The ThndrX half is not finished, and cannot be: filling an order ticket needs the
real DOM, and inventing selectors would produce an adapter that silently clicks
the wrong thing. `AssistedAdapter` therefore raises a clear NotImplementedError
naming what it needs, rather than pretending. Everything around it — tickets,
approval, expiry, idempotency, the audit trail — is complete and tested, so
finishing it is one focused pass against a logged-in session.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from egx_trader.config import ExecutionMode
from egx_trader.execution.tickets import Ticket, TicketState
from egx_trader.market_calendar import CAIRO


class ExecutionError(RuntimeError):
    """Submission failed."""


@dataclass
class SubmitResult:
    ticket: Ticket
    broker_ref: str = ""
    screenshot: str = ""
    message: str = ""

    @property
    def placed(self) -> bool:
        return self.ticket.state.resulted_in_an_order


@runtime_checkable
class ExecutionAdapter(Protocol):
    mode: ExecutionMode

    def submit(self, ticket: Ticket) -> SubmitResult:
        """Act on an APPROVED ticket. Never on a pending one."""
        ...


def _require_approved(ticket: Ticket) -> None:
    """Adapters only ever act on an approved ticket.

    Checked here rather than trusted, because this is the last point before an
    order reaches a broker and the cost of getting it wrong is a real position.
    """
    if ticket.state is not TicketState.APPROVED:
        raise ExecutionError(
            f"ticket {ticket.id} is {ticket.state.value}, not approved — refusing to "
            "submit. Only an approved ticket may be sent."
        )


@dataclass
class AlertAdapter:
    """Emits the ticket and stops. No browser, no order path at all.

    The default mode. Everything the operator needs is in the summary; they place
    the order themselves in Thndr.
    """

    mode: ExecutionMode = ExecutionMode.ALERT
    sent: list[Ticket] = field(default_factory=list)

    def submit(self, ticket: Ticket) -> SubmitResult:
        _require_approved(ticket)
        self.sent.append(ticket)
        # Deliberately NOT marked placed: this system did not place anything. Saying
        # otherwise would leave the ledger believing it holds a position it does not.
        return SubmitResult(
            ticket=ticket,
            message=(
                "Alert mode — nothing was sent to the broker. Place this yourself:\n"
                f"  {ticket.summary()}"
            ),
        )


@dataclass
class AssistedAdapter:
    """Pre-fills the ThndrX ticket and stops before confirm.

    NOT FINISHED. Needs one logged-in session so the order-entry DOM can be read;
    guessing selectors risks filling the wrong field or clicking the wrong button,
    which on this surface means a real trade. Failing loudly is the correct
    behaviour until then.
    """

    mode: ExecutionMode = ExecutionMode.ASSISTED
    profile_dir: object | None = None

    def submit(self, ticket: Ticket) -> SubmitResult:
        _require_approved(ticket)
        raise NotImplementedError(
            "AssistedAdapter needs the ThndrX order-entry DOM, which can only be "
            "read from a logged-in session. Run `egx thndrx-login`, then this is one "
            "focused pass. Guessing selectors here would risk clicking the wrong "
            "control on a live account."
        )


@dataclass
class AutoAdapter:
    """Confirms without a human.

    Double-gated in config (EGX_EXECUTION_MODE=auto AND
    EGX_I_UNDERSTAND_LIVE_TRADING=true) and capped per order. Built on the same
    ticket lifecycle as the others, so it still cannot submit an unapproved ticket
    — "auto" means the approval is automatic, not that approval is skipped.
    """

    mode: ExecutionMode = ExecutionMode.AUTO
    inner: ExecutionAdapter | None = None

    def submit(self, ticket: Ticket) -> SubmitResult:
        _require_approved(ticket)
        if self.inner is None:
            raise NotImplementedError(
                "Auto mode has no broker adapter yet — it depends on the assisted "
                "adapter's order-entry path, which needs a logged-in ThndrX session."
            )
        return self.inner.submit(ticket)


def auto_approve(ticket: Ticket, *, max_order_egp: float) -> bool:
    """Approve a ticket without a human, if it is inside its cap.

    Separate from the adapter so the decision is auditable on its own, and so a
    test can assert that an over-cap order is refused rather than shrunk.
    """
    if ticket.order.notional > max_order_egp:
        return False
    ticket.approve(dt.datetime.now(CAIRO), by="auto")
    return True


def build_adapter(mode: ExecutionMode) -> ExecutionAdapter:
    """The adapter for a mode. Unknown modes raise rather than defaulting.

    Defaulting to alert would be the safe direction, but it would also mean a
    misconfigured process quietly not trading while appearing to work.
    """
    if mode is ExecutionMode.ALERT:
        return AlertAdapter()
    if mode is ExecutionMode.ASSISTED:
        return AssistedAdapter()
    if mode is ExecutionMode.AUTO:
        return AutoAdapter()
    raise ValueError(f"no adapter for execution mode {mode!r}")
