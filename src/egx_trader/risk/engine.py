"""Pre-trade risk gates.

Every order passes through here before it exists. The gates are deliberately
boring and deliberately absolute: each one blocks, and a blocked order is not
negotiated down, because a system that shrinks an order until it passes has no
limits at all.

**The overriding rule: nothing here may prevent an exit.** Every gate is an entry
gate. A cap that blocks a sell does not reduce risk, it creates it — you would be
holding a position the software refuses to let go of. Downside protection comes
from broker-resting stop orders, which work whether or not this process is even
running.

That rule was not obvious and cost a test to find: a 50,000 EGP exit was being
blocked by the 10,000 EGP per-order cap, which would have meant a position that
could never be liquidated in one order. The same reasoning then applied to the
monthly execution budget (running out of free trades must not stop you selling),
the daily loss limit (being down 4% is when selling matters most), and the kill
switch (stopping new risk is not the same as trapping existing risk).

Exits are therefore checked against exactly one thing: **settlement**. And that is
not really this engine's rule — the broker rejects a sale of unsettled shares
regardless, so blocking it here just produces a better error message.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum

from egx_trader.execution.orders import Order, Side
from egx_trader.portfolio.ledger import Ledger
from egx_trader.universe import Instrument


class Verdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class Gate(StrEnum):
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS = "daily_loss"
    ORDER_SIZE = "order_size"
    POSITION_CONCENTRATION = "position_concentration"
    SETTLEMENT = "settlement"
    LIQUIDITY = "liquidity"
    CASH = "cash"
    ORDER_BUDGET = "order_budget"
    NEW_POSITIONS = "new_positions"
    UNIVERSE = "universe"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    verdict: Verdict
    blocked_by: list[Gate] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def explain(self) -> str:
        if self.allowed:
            return "allowed"
        return "; ".join(self.reasons)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_egp: float = 10_000
    max_position_pct: float = 25.0
    max_sector_pct: float = 40.0
    max_new_positions_per_day: int = 3
    daily_loss_limit_pct: float = 4.0
    grandfather_existing: bool = True
    free_trades_per_month: int = 50
    exit_reserve: int = 10
    """Executions held back from the free allowance so exits are never blocked."""

    max_volume_share: float = 0.05
    """Cap on a single order as a fraction of the name's average daily volume."""


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything the gates need, gathered once so they stay pure."""

    when: dt.date
    prices: dict[str, float]
    instrument: Instrument | None = None
    avg_daily_volume: int | None = None
    executions_this_month: int = 0
    new_positions_today: int = 0
    day_start_equity: float | None = None
    kill_switch: bool = False


Finding = tuple[Gate, str]


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def check(self, order: Order, ledger: Ledger, ctx: RiskContext) -> RiskDecision:
        """Run every gate. All failures are collected, not just the first.

        Reporting only the first would mean fixing one problem and rediscovering
        the next on the following attempt.
        """
        price = order.limit_price or order.stop_price or ctx.prices.get(order.symbol)
        notional = (price or 0) * order.quantity

        # Exits get the settlement check and nothing else. See the module docstring:
        # a gate that blocks a sell creates risk rather than reducing it.
        if order.side is Side.SELL:
            findings: list[Finding] = self._sell_gates(order, ledger, ctx)
        else:
            findings = [
                *self._entry_wide(ledger, ctx, notional),
                *self._buy_gates(order, ledger, ctx, notional, price),
            ]

        if not findings:
            return RiskDecision(Verdict.ALLOW)
        return RiskDecision(
            Verdict.BLOCK,
            [gate for gate, _ in findings],
            [reason for _, reason in findings],
        )

    # ── entry-wide gates ─────────────────────────────────────────────────────

    def _entry_wide(self, ledger: Ledger, ctx: RiskContext, notional: float) -> list[Finding]:
        """Conditions that stop the system taking ON risk. Never applied to exits."""
        limits = self.limits
        out: list[Finding] = []

        if ctx.kill_switch:
            out.append((Gate.KILL_SWITCH, "kill switch is engaged — no new positions"))

        if ctx.day_start_equity:
            equity = ledger.equity(ctx.prices)
            drawdown = (ctx.day_start_equity - equity) / ctx.day_start_equity * 100
            if drawdown >= limits.daily_loss_limit_pct:
                out.append(
                    (
                        Gate.DAILY_LOSS,
                        f"down {drawdown:.1f}% today, limit {limits.daily_loss_limit_pct:.1f}%",
                    )
                )

        if notional > limits.max_order_egp:
            out.append(
                (
                    Gate.ORDER_SIZE,
                    f"order {notional:,.0f} EGP exceeds the {limits.max_order_egp:,.0f} cap",
                )
            )
        return out

    # ── exits ────────────────────────────────────────────────────────────────

    def _sell_gates(self, order: Order, ledger: Ledger, ctx: RiskContext) -> list[Finding]:
        """The only gate an exit faces.

        Not a policy choice so much as a courtesy: the broker rejects a sale of
        unsettled shares anyway, so catching it here just produces a clearer error
        than a rejection from ThndrX would.
        """
        sellable = ledger.sellable_quantity(order.symbol, ctx.when)
        if order.quantity <= sellable:
            return []
        unsettled = ledger.unsettled_quantity(order.symbol, ctx.when)
        return [
            (
                Gate.SETTLEMENT,
                f"only {sellable} of {ledger.quantity(order.symbol)} shares have "
                f"settled ({unsettled} unsettled)",
            )
        ]

    # ── entries ──────────────────────────────────────────────────────────────

    def _buy_gates(
        self,
        order: Order,
        ledger: Ledger,
        ctx: RiskContext,
        notional: float,
        price: float | None,
    ) -> list[Finding]:
        limits = self.limits
        out: list[Finding] = []

        if ctx.instrument is not None and not ctx.instrument.is_tradable:
            out.append((Gate.UNIVERSE, f"{order.symbol} is {ctx.instrument.status.value}"))

        if notional > ledger.cash_egp:
            out.append(
                (
                    Gate.CASH,
                    f"needs {notional:,.0f} EGP, have {ledger.cash_egp:,.0f} "
                    "(unsettled proceeds are not spendable)",
                )
            )

        budget = limits.free_trades_per_month - limits.exit_reserve
        if ctx.executions_this_month >= budget:
            out.append(
                (
                    Gate.ORDER_BUDGET,
                    f"{ctx.executions_this_month} executions this month; entries stop "
                    f"at {budget} to keep {limits.exit_reserve} free for exits",
                )
            )

        if ctx.new_positions_today >= limits.max_new_positions_per_day:
            out.append(
                (
                    Gate.NEW_POSITIONS,
                    f"already opened {ctx.new_positions_today} positions today",
                )
            )

        if ctx.avg_daily_volume and price:
            cap = int(ctx.avg_daily_volume * limits.max_volume_share)
            if order.quantity > cap:
                out.append(
                    (
                        Gate.LIQUIDITY,
                        f"{order.quantity} shares is more than "
                        f"{limits.max_volume_share:.0%} of average volume "
                        f"({cap} shares)",
                    )
                )

        out.extend(self._concentration(order, ledger, ctx, notional, price))
        return out

    def _concentration(
        self,
        order: Order,
        ledger: Ledger,
        ctx: RiskContext,
        notional: float,
        price: float | None,
    ) -> list[Finding]:
        """Measured on the RESULTING position, not the current one.

        Grandfathering exempts a holding that predates the bot from being forced
        down, but it never lets the bot ADD to an already over-weight position —
        otherwise the exemption becomes a licence to concentrate further.
        """
        limits = self.limits
        equity = ledger.equity(ctx.prices)
        if equity <= 0 or not price:
            return []

        held_price = ctx.prices.get(order.symbol, price)
        after = (ledger.quantity(order.symbol) * held_price + notional) / equity * 100
        if after <= limits.max_position_pct:
            return []
        return [
            (
                Gate.POSITION_CONCENTRATION,
                f"would take {order.symbol} to {after:.0f}% of equity, "
                f"limit {limits.max_position_pct:.0f}%",
            )
        ]
