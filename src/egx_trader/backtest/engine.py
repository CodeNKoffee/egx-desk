"""Event-driven backtest with the EGX/Thndr cost model.

The engine's job is to be pessimistic in the specific ways this market is, because
a backtest that is optimistic in any of them produces a number that cannot be
traded:

**No look-ahead, structurally.** A strategy sees bars 0..i and the fill happens at
the open of bar i+1. It is not asked to behave; it is never handed bar i+1.

**Fills respect EGX's price band.** A stock can move about 20% in a session, so an
order cannot fill through a limit-up. Modelling a clean fill at any price is how a
breakout system posts imaginary returns on exactly its best days.

**Liquidity caps the size.** Filling more than a few percent of a day's actual
volume is fantasy on a market where many names trade under 500k EGP.

**Settlement locks the position.** T+2 by default: a lot bought Sunday cannot be
sold until Tuesday, so an exit signal on Monday executes later than it looks.

**Stops fill at the next available price, not at the stop.** Thndr's stop triggers
a MARKET sell. Modelling it as a clean fill at the stop price is the single most
common way a backtest flatters itself, and it matters most on gaps — precisely
when stops trigger.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from egx_trader.backtest.costs import CostModel
from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.market_calendar import EGXCalendar
from egx_trader.strategies.base import Intent, Signal, Strategy

EGX_DAILY_BAND = 0.20
DEFAULT_VOLUME_CAP = 0.05
DEFAULT_SLIPPAGE = 0.002


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    entry_date: dt.date
    entry_price: float
    exit_date: dt.date
    exit_price: float
    quantity: int
    entry_reason: str
    exit_reason: str
    commission: float
    settlement_delayed_days: int = 0

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commission

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.quantity
        return (self.net_pnl / cost * 100) if cost else 0.0

    @property
    def bars_held(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    trades: list[Trade] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    cost_model: CostModel = field(default_factory=CostModel)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def commission_paid(self) -> float:
        return sum(t.commission for t in self.trades)

    @property
    def tax(self) -> float:
        return self.cost_model.tax_on(self.gross_pnl - self.commission_paid)

    @property
    def net_pnl(self) -> float:
        """After commission and CGT. Subscription is charged at portfolio level."""
        return self.gross_pnl - self.commission_paid - self.tax

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.trades if t.net_pnl > 0]

    @property
    def losses(self) -> list[Trade]:
        return [t for t in self.trades if t.net_pnl <= 0]

    @property
    def hit_rate(self) -> float:
        return len(self.wins) / len(self.trades) if self.trades else 0.0

    @property
    def best(self) -> Trade | None:
        return max(self.trades, key=lambda t: t.return_pct) if self.trades else None

    def summary(self) -> str:
        if not self.trades:
            return f"{self.symbol} / {self.strategy}: no trades"
        return (
            f"{self.symbol} / {self.strategy}: {len(self.trades)} trades, "
            f"hit {self.hit_rate:.0%}, net {self.net_pnl:,.0f} EGP"
        )


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    capital_egp: float = 20_000
    volume_cap: float = DEFAULT_VOLUME_CAP
    slippage: float = DEFAULT_SLIPPAGE
    settlement_sessions: int = 2
    price_band: float = EGX_DAILY_BAND


def run_backtest(
    series: OHLCVSeries,
    strategy: Strategy,
    *,
    config: BacktestConfig | None = None,
    calendar: EGXCalendar | None = None,
    cost_model: CostModel | None = None,
) -> BacktestResult:
    """Replay a strategy over one symbol."""
    cfg = config or BacktestConfig()
    cal = calendar or EGXCalendar(strict=False)
    costs = cost_model or CostModel()
    result = BacktestResult(symbol=series.symbol, strategy=strategy.name, cost_model=costs)

    strategy.prepare(series)
    candles = series.candles

    in_position = False
    entry_index: int | None = None
    entry_price = 0.0
    entry_date: dt.date | None = None
    entry_reason = ""
    quantity = 0
    settle_date: dt.date | None = None

    for i in range(len(candles) - 1):
        intent: Intent = strategy.evaluate(
            series, i, in_position=in_position, entry_index=entry_index
        )
        nxt = candles[i + 1]

        if intent.signal is Signal.ENTER and not in_position:
            fill = _fill_price(candles[i].close, nxt, cfg, buying=True)
            if fill is None:
                result.skipped.append(f"{nxt.date}: entry unfillable inside the price band")
                continue
            size = _position_size(cfg.capital_egp, fill, nxt.volume, cfg.volume_cap)
            if size <= 0:
                result.skipped.append(f"{nxt.date}: too illiquid to size a position")
                continue
            in_position = True
            entry_index, entry_price, entry_date = i + 1, fill, nxt.date
            entry_reason, quantity = intent.reason, size
            settle_date = cal.add_sessions(nxt.date, cfg.settlement_sessions)

        elif intent.signal is Signal.EXIT and in_position and entry_date is not None:
            trade = _close_position(
                series=series,
                candles=candles,
                i=i,
                cfg=cfg,
                costs=costs,
                settle_date=settle_date,
                entry_date=entry_date,
                entry_price=entry_price,
                entry_reason=entry_reason,
                quantity=quantity,
                exit_reason=intent.reason,
                skipped=result.skipped,
            )
            if trade is None:
                continue
            result.trades.append(trade)
            in_position = False
            entry_index = None
            settle_date = None

    return result


def _close_position(
    *,
    series: OHLCVSeries,
    candles: tuple[Candle, ...],
    i: int,
    cfg: BacktestConfig,
    costs: CostModel,
    settle_date: dt.date | None,
    entry_date: dt.date,
    entry_price: float,
    entry_reason: str,
    quantity: int,
    exit_reason: str,
    skipped: list[str],
) -> Trade | None:
    """Resolve an exit, respecting settlement. Returns None if it cannot fill."""
    nxt = candles[i + 1]
    delayed = 0
    exit_bar = nxt

    # T+2: the shares are not deliverable yet, so the sell waits for settlement.
    if settle_date is not None and nxt.date < settle_date:
        delayed = (settle_date - nxt.date).days
        exit_index = _index_on_or_after(candles, settle_date, i + 1)
        if exit_index is None:
            return None
        exit_bar = candles[exit_index]

    fill = _fill_price(candles[i].close, exit_bar, cfg, buying=False)
    if fill is None:
        skipped.append(f"{exit_bar.date}: exit unfillable inside the price band")
        return None

    commission = costs.commission(entry_date, entry_price * quantity) + costs.commission(
        exit_bar.date, fill * quantity
    )
    return Trade(
        symbol=series.symbol,
        entry_date=entry_date,
        entry_price=entry_price,
        exit_date=exit_bar.date,
        exit_price=fill,
        quantity=quantity,
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        commission=commission,
        settlement_delayed_days=delayed,
    )


def _fill_price(
    prev_close: float, bar: Candle, cfg: BacktestConfig, *, buying: bool
) -> float | None:
    """Fill at the next open, with slippage, clamped to EGX's daily band.

    A limit-up open cannot be bought into: the band caps how far price may move
    from the prior close, and an order queued behind it does not fill.
    """
    open_price = bar.open
    limit_up = prev_close * (1 + cfg.price_band)
    limit_down = prev_close * (1 - cfg.price_band)

    if buying and open_price >= limit_up:
        return None
    if not buying and open_price <= limit_down:
        return None

    slip = cfg.slippage if buying else -cfg.slippage
    return round(open_price * (1 + slip), 4)


def _position_size(capital: float, price: float, bar_volume: int, cap: float) -> int:
    """Whole shares, capped by both capital and a share of the day's real volume."""
    if price <= 0:
        return 0
    by_capital = int(capital // price)
    by_volume = int(bar_volume * cap)
    return max(0, min(by_capital, by_volume))


def _index_on_or_after(candles: tuple[Candle, ...], when: dt.date, start: int) -> int | None:
    for j in range(start, len(candles)):
        if candles[j].date >= when:
            return j
    return None
