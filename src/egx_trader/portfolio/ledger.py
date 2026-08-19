"""Lot-level portfolio ledger.

Lots, not net positions. Three EGX facts make that mandatory:

**Settlement is per lot.** A T+2 buy on Sunday is not deliverable until Tuesday.
Netting positions loses the acquisition date, and with it the only way to know how
much of a holding is actually sellable today. A system that gets this wrong tries
to sell shares it does not yet have.

**CGT is on realised gains**, so which shares left matters. FIFO is assumed and
named, rather than being an accident of dict ordering.

**Whole-book mode means pre-existing positions.** Lots opened before the bot
existed have no known acquisition date, so they are marked settled — they plainly
are — but flagged, so nothing later mistakes an assumption for a record.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from egx_trader.backtest.costs import CostModel
from egx_trader.market_calendar import EGXCalendar

SETTLEMENT_SESSIONS = 2


@dataclass
class Lot:
    """Shares bought together, tracked separately until sold."""

    symbol: str
    quantity: int
    price: float
    trade_date: dt.date | None
    settle_date: dt.date | None
    pre_existing: bool = False
    """Opened before the bot. Treated as settled, but the date is unknown, so the
    distinction is kept rather than silently assumed away."""

    @property
    def cost(self) -> float:
        return round(self.quantity * self.price, 2)

    def settled_on(self, when: dt.date) -> bool:
        if self.pre_existing or self.settle_date is None:
            return True
        return when >= self.settle_date


@dataclass
class RealisedTrade:
    symbol: str
    quantity: int
    entry_price: float
    exit_price: float
    exit_date: dt.date
    commission: float = 0.0

    @property
    def gross_pnl(self) -> float:
        return round((self.exit_price - self.entry_price) * self.quantity, 2)

    @property
    def net_pnl(self) -> float:
        return round(self.gross_pnl - self.commission, 2)


class InsufficientSharesError(RuntimeError):
    """Tried to sell more than is held, or more than has settled."""


@dataclass
class Ledger:
    """Positions, cash and realised P&L. The one store that cannot be rebuilt."""

    cash_egp: float = 0.0
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    realised: list[RealisedTrade] = field(default_factory=list)
    calendar: EGXCalendar = field(default_factory=lambda: EGXCalendar(strict=False))
    costs: CostModel = field(default_factory=CostModel)

    # ── positions ────────────────────────────────────────────────────────────

    def quantity(self, symbol: str) -> int:
        return sum(lot.quantity for lot in self.lots.get(symbol, []))

    def sellable_quantity(self, symbol: str, when: dt.date) -> int:
        """Shares deliverable today. Unsettled lots are held, not counted.

        This is the number an exit must size against — using the total is how a
        system places a sell the broker will reject.
        """
        return sum(lot.quantity for lot in self.lots.get(symbol, []) if lot.settled_on(when))

    def unsettled_quantity(self, symbol: str, when: dt.date) -> int:
        return self.quantity(symbol) - self.sellable_quantity(symbol, when)

    def average_cost(self, symbol: str) -> float | None:
        held = self.lots.get(symbol, [])
        total = sum(lot.quantity for lot in held)
        if not total:
            return None
        return round(sum(lot.cost for lot in held) / total, 4)

    def book_cost(self, symbol: str | None = None) -> float:
        if symbol is not None:
            return round(sum(lot.cost for lot in self.lots.get(symbol, [])), 2)
        return round(sum(lot.cost for held in self.lots.values() for lot in held), 2)

    def symbols(self) -> list[str]:
        return sorted(s for s, held in self.lots.items() if held)

    # ── mutations ────────────────────────────────────────────────────────────

    def add_pre_existing(self, symbol: str, quantity: int, avg_cost: float) -> Lot:
        """Seed a holding that predates the bot. Cash is not touched."""
        lot = Lot(
            symbol=symbol,
            quantity=quantity,
            price=avg_cost,
            trade_date=None,
            settle_date=None,
            pre_existing=True,
        )
        self.lots.setdefault(symbol, []).append(lot)
        return lot

    def buy(self, symbol: str, quantity: int, price: float, when: dt.date) -> Lot:
        notional = quantity * price
        commission = self.costs.commission(when, notional)
        self.cash_egp -= notional + commission
        lot = Lot(
            symbol=symbol,
            quantity=quantity,
            price=price,
            trade_date=when,
            settle_date=self.calendar.add_sessions(when, SETTLEMENT_SESSIONS),
        )
        self.lots.setdefault(symbol, []).append(lot)
        return lot

    def sell(self, symbol: str, quantity: int, price: float, when: dt.date) -> list[RealisedTrade]:
        """Sell FIFO from settled lots only.

        Raises rather than partially filling: a silent short-fill would leave the
        ledger and the broker disagreeing, and reconciliation would then halt on a
        drift the system itself caused.
        """
        available = self.sellable_quantity(symbol, when)
        if quantity > available:
            unsettled = self.unsettled_quantity(symbol, when)
            raise InsufficientSharesError(
                f"{symbol}: asked to sell {quantity}, only {available} settled"
                + (f" ({unsettled} still unsettled)" if unsettled else "")
            )

        notional = quantity * price
        commission = self.costs.commission(when, notional)
        self.cash_egp += notional - commission

        remaining = quantity
        realised: list[RealisedTrade] = []
        held = self.lots.get(symbol, [])
        # FIFO, and only from lots that have settled.
        for lot in [lot for lot in held if lot.settled_on(when)]:
            if remaining <= 0:
                break
            take = min(remaining, lot.quantity)
            realised.append(
                RealisedTrade(
                    symbol=symbol,
                    quantity=take,
                    entry_price=lot.price,
                    exit_price=price,
                    exit_date=when,
                    # Charge the whole execution's commission once, to the first
                    # slice, rather than inventing a per-lot split.
                    commission=commission if not realised else 0.0,
                )
            )
            lot.quantity -= take
            remaining -= take

        self.lots[symbol] = [lot for lot in held if lot.quantity > 0]
        self.realised.extend(realised)
        return realised

    # ── valuation ────────────────────────────────────────────────────────────

    def market_value(self, prices: dict[str, float]) -> float:
        return round(
            sum(
                self.quantity(symbol) * prices[symbol]
                for symbol in self.symbols()
                if symbol in prices
            ),
            2,
        )

    def equity(self, prices: dict[str, float]) -> float:
        return round(self.cash_egp + self.market_value(prices), 2)

    def unrealised_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for symbol in self.symbols():
            price = prices.get(symbol)
            if price is None:
                continue
            for lot in self.lots[symbol]:
                total += (price - lot.price) * lot.quantity
        return round(total, 2)

    def realised_pnl(self) -> float:
        return round(sum(t.net_pnl for t in self.realised), 2)

    def tax_reserve(self) -> float:
        """CGT owed on realised gains so far.

        Held back as a reserve rather than treated as free cash: it is spent money
        that has not left the account yet, and a system that counts it as buying
        power is levered by exactly the tax bill.
        """
        return round(self.costs.tax_on(self.realised_pnl()), 2)

    def concentration(self, prices: dict[str, float]) -> dict[str, float]:
        """Each position as a percent of equity."""
        equity = self.equity(prices)
        if equity <= 0:
            return {}
        return {
            symbol: round(self.quantity(symbol) * prices[symbol] / equity * 100, 2)
            for symbol in self.symbols()
            if symbol in prices
        }
