"""Breakout momentum — the strategy this project exists to test.

The existing scanner buys weakness: it emits BUY only when `rsi <= 30` or
`rsi <= 40`. Through BIOC's run from ~48 to ~609, RSI sat in the 70s-90s and price
was far above both moving averages, so every rule fell through to HOLD. It is
structurally incapable of buying strength.

This buys strength, and — more importantly — holds it. Three entry conditions:

1. **Close above the prior 55-session high.** The channel excludes today, so this
   is a genuine new high rather than a tautology.
2. **Relative volume at least 2.5x.** A breakout without volume is a quote.
3. **EGP turnover above a floor.** Share count is meaningless across EGX; a name
   trading 50k EGP a day cannot absorb a position at any size.

The exit is the part that matters. A chandelier stop at 3xATR below the running
high, ratcheting up and never down, with **no profit target at all**. A fixed
target is what turns an 866% move into a 30% winner — the entry gets you into
maybe one BIOC a year, the exit decides whether that pays for the losers.
"""

from __future__ import annotations

from dataclasses import dataclass

from egx_trader.data.models import OHLCVSeries
from egx_trader.features.indicators import (
    atr,
    chandelier_stop,
    donchian,
    relative_volume,
    sma,
    turnover_egp,
)
from egx_trader.strategies.base import Intent, Signal


@dataclass(frozen=True, slots=True)
class BreakoutParams:
    channel: int = 55
    atr_period: int = 14
    atr_multiple: float = 3.0
    min_relative_volume: float = 2.5
    min_turnover_egp: float = 500_000
    trend_fast: int = 20
    trend_slow: int = 50


class BreakoutMomentum:
    name = "breakout_momentum"

    def __init__(self, params: BreakoutParams | None = None) -> None:
        self.p = params or BreakoutParams()
        self._up: list[float | None] = []
        self._atr: list[float | None] = []
        self._rvol: list[float | None] = []
        self._turnover: list[float | None] = []
        self._fast: list[float | None] = []
        self._slow: list[float | None] = []

    def prepare(self, series: OHLCVSeries) -> None:
        p = self.p
        self._up, _ = donchian(series, p.channel)
        self._atr = atr(series, p.atr_period)
        self._rvol = relative_volume(series)
        self._turnover = turnover_egp(series)
        closes = series.closes
        self._fast = sma(closes, p.trend_fast)
        self._slow = sma(closes, p.trend_slow)

    def evaluate(
        self, series: OHLCVSeries, i: int, *, in_position: bool, entry_index: int | None
    ) -> Intent:
        if in_position and entry_index is not None:
            return self._manage(series, i, entry_index)
        return self._look_for_entry(series, i)

    def _manage(self, series: OHLCVSeries, i: int, entry_index: int) -> Intent:
        """Hold while the trailing stop holds. No profit target, ever."""
        bar = series.candles[i]
        stop = chandelier_stop(
            series, entry_index, i, atr_values=self._atr, multiple=self.p.atr_multiple
        )
        if stop is not None and bar.close <= stop:
            return Intent(
                Signal.EXIT,
                bar.date,
                f"close {bar.close:.2f} broke the chandelier stop at {stop:.2f}",
                stop_price=stop,
                features={"close": bar.close, "stop": stop},
            )
        return Intent(Signal.HOLD, bar.date, "trailing", stop_price=stop)

    def _look_for_entry(self, series: OHLCVSeries, i: int) -> Intent:
        bar = series.candles[i]
        channel = self._up[i]
        rvol = self._rvol[i]
        turnover = self._turnover[i]
        fast, slow = self._fast[i], self._slow[i]
        current_atr = self._atr[i]

        # A missing indicator is missing, never zero. Warm-up must not trade.
        if (
            channel is None
            or rvol is None
            or turnover is None
            or fast is None
            or slow is None
            or current_atr is None
        ):
            return Intent(Signal.HOLD, bar.date, "warming up")

        features = {
            "close": bar.close,
            "channel_high": channel,
            "relative_volume": rvol,
            "turnover_egp": turnover,
            "sma_fast": fast,
            "sma_slow": slow,
            "atr": current_atr,
        }

        blocked = self._entry_blocker(features)
        if blocked:
            return Intent(Signal.HOLD, bar.date, blocked, features=features)

        return Intent(
            Signal.ENTER,
            bar.date,
            f"closed {bar.close:.2f} above the {self.p.channel}-day high "
            f"{channel:.2f} on {rvol:.1f}x volume",
            stop_price=bar.close - self.p.atr_multiple * current_atr,
            features=features,
        )

    def _entry_blocker(self, f: dict[str, float]) -> str:
        """Why this bar is not an entry, or empty if it is one."""
        if f["close"] <= f["channel_high"]:
            return "no breakout"
        if f["relative_volume"] < self.p.min_relative_volume:
            return f"breakout on thin volume ({f['relative_volume']:.1f}x)"
        if f["turnover_egp"] < self.p.min_turnover_egp:
            return f"too illiquid ({f['turnover_egp']:,.0f} EGP/day)"
        if f["sma_fast"] <= f["sma_slow"]:
            return "trend not up"
        return ""
