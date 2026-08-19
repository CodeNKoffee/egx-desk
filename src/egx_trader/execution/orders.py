"""Order types and the rules for choosing between them.

Thndr offers market orders, limit orders, and stop orders (a stop price that
triggers a market sell, on the Trader plan). Which to use is not a preference —
EGX's microstructure decides it, and getting it wrong is expensive in ways that
do not show up until a fill prints.

Four facts drive every rule here:

**Price limits and halts.** EGX caps a session at roughly ±20% on the most-active
board, tighter elsewhere, with a ±10% MVWAP circuit breaker that halts a stock for
10 minutes. The band is visibly binding — BIOC printed exactly -20.0% then +20.0%
on consecutive sessions during its 2026 run. A market order into a name that is
running can therefore be queued into a halt and filled on the other side of it.

**Thin books.** Median EGX turnover outside the index names is small. A market
order walks the book, and on a stock that trades a few hundred thousand EGP a day
it walks it a long way.

**T+0 requires a price.** Same-session selling on Thndr goes through the advanced
limit screen and will not accept a market order at all. So a T+0 exit is a limit
order or it does not exist.

**Stops trigger a market sell.** Thndr's stop order fires at market once the stop
price trades. That is the right trade-off for protection — it prioritises getting
out over getting a price — but it means the fill can be well below the stop after
a gap or a post-halt reopen, and the backtest must model it that way.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from egx_trader.universe.models import Instrument


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    """Immediate, no price control. Only for liquid names, and never on entry."""

    LIMIT = "limit"
    """A price ceiling (buy) or floor (sell). The default for everything here."""

    STOP = "stop"
    """A resting stop price that triggers a MARKET sell. Protection, not precision."""


class Settlement(StrEnum):
    T0 = "t0"
    """Same session. Needs an eligible board, a Trader plan, and an advanced limit."""

    T1 = "t1"
    T2 = "t2"
    """The default. A lot bought Sunday cannot be sold until Tuesday."""


class Urgency(StrEnum):
    PATIENT = "patient"
    """Willing to miss the fill to get the price. Entries are patient."""

    NORMAL = "normal"

    URGENT = "urgent"
    """Willing to pay to get out. Risk exits are urgent."""


class Order(BaseModel):
    """An order ticket. Constructing one does not place it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    side: Side
    order_type: OrderType
    quantity: Annotated[int, Field(gt=0)]

    limit_price: Annotated[float, Field(gt=0)] | None = None
    stop_price: Annotated[float, Field(gt=0)] | None = None
    settlement: Settlement = Settlement.T2

    reason: str = ""
    created_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _check_prices(self) -> Order:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError(f"{self.symbol}: a limit order needs a limit price")
        if self.order_type is OrderType.STOP and self.stop_price is None:
            raise ValueError(f"{self.symbol}: a stop order needs a stop price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError(f"{self.symbol}: a market order cannot carry a limit price — pick one")
        if self.settlement is Settlement.T0 and self.order_type is not OrderType.LIMIT:
            raise ValueError(
                f"{self.symbol}: T+0 goes through Thndr's advanced limit screen, which "
                "requires a price. A same-session exit is a limit order or it is nothing."
            )
        return self

    @property
    def notional(self) -> float:
        """Best estimate of the order's value. Market orders have no price to use."""
        price = self.limit_price or self.stop_price
        return round(self.quantity * price, 2) if price else 0.0


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Knobs for `choose_order_type`. Defaults are deliberately conservative."""

    entry_limit_offset_pct: float = 0.5
    """How far through the last price to place a breakout entry. Enough to cross the
    spread on a moving stock, not enough to chase it into a halt."""

    urgent_exit_offset_pct: float = 3.0
    """A sell limit placed BELOW the last price still executes — it just caps how
    far down. This buys most of a market order's certainty without its worst fill."""

    min_daily_turnover_egp: float = 500_000
    """Below this a market order walks a thin book. Verified against the universe:
    plenty of EGX names trade under this on an ordinary day."""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    order_type: OrderType
    limit_price: float | None
    rationale: str


def choose_order_type(
    *,
    side: Side,
    urgency: Urgency,
    last_price: float,
    instrument: Instrument | None = None,
    avg_daily_turnover_egp: float | None = None,
    settlement: Settlement = Settlement.T2,
    policy: RoutingPolicy | None = None,
) -> RoutingDecision:
    """Pick an order type and price from the situation.

    The bias is toward limit orders throughout. A missed entry costs an
    opportunity; a bad fill costs money, and on EGX's thin books a market order on
    the wrong name can cost several percent before the position even starts.
    """
    p = policy or RoutingPolicy()

    if settlement is Settlement.T0:
        # Not a judgement call — Thndr's advanced order screen requires a price.
        offset = p.urgent_exit_offset_pct if urgency is Urgency.URGENT else 0.0
        price = round(last_price * (1 - offset / 100), 2)
        return RoutingDecision(
            OrderType.LIMIT,
            price,
            "T+0 must route through the advanced limit screen, which will not accept "
            "a market order",
        )

    liquid = (
        avg_daily_turnover_egp is not None and avg_daily_turnover_egp >= p.min_daily_turnover_egp
    )

    if side is Side.BUY:
        # Entries are never market orders. A breakout is by definition a moment when
        # the book is moving, which is exactly when a market order fills worst.
        price = round(last_price * (1 + p.entry_limit_offset_pct / 100), 2)
        return RoutingDecision(
            OrderType.LIMIT,
            price,
            f"entry priced {p.entry_limit_offset_pct}% through last — enough to cross "
            "a moving spread, not enough to chase into a halt",
        )

    if urgency is Urgency.URGENT:
        if liquid:
            return RoutingDecision(
                OrderType.MARKET,
                None,
                "urgent exit on a liquid name — certainty of exit is worth the spread",
            )
        price = round(last_price * (1 - p.urgent_exit_offset_pct / 100), 2)
        return RoutingDecision(
            OrderType.LIMIT,
            price,
            f"urgent exit, but turnover is thin, so a limit {p.urgent_exit_offset_pct}% "
            "below last caps the damage while still executing",
        )

    price = round(last_price, 2)
    return RoutingDecision(
        OrderType.LIMIT, price, "ordinary exit priced at last — no reason to pay up"
    )


def protective_stop(
    *,
    symbol: str,
    quantity: int,
    stop_price: float,
    reason: str = "",
) -> Order:
    """A resting stop that the broker enforces.

    This is the most valuable thing the Thndr Trader subscription unlocks. The bot
    recomputes the level once per pre-open and re-places it; between those updates
    the broker holds it. A crashed process, a sleeping Mac or an expired browser
    session therefore cannot leave a position unprotected — which is the difference
    between a hobby script and something that can hold most of a net worth.
    """
    return Order(
        symbol=symbol,
        side=Side.SELL,
        order_type=OrderType.STOP,
        quantity=quantity,
        stop_price=round(stop_price, 2),
        reason=reason or "protective stop, enforced by the broker between updates",
    )
