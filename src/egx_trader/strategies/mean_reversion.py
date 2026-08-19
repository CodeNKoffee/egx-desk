"""The predecessor scanner's rules, ported verbatim as a baseline to beat.

Here to be measured, not used: if the breakout strategy cannot beat a dip-buyer
net of costs, that is worth knowing before any capital moves.

It should also demonstrate the structural claim — that this family cannot buy a
breakout at all. Through a parabolic run, RSI is high and price is above both
moving averages, so every branch falls through to HOLD.
"""

from __future__ import annotations

from dataclasses import dataclass

from egx_trader.data.models import OHLCVSeries
from egx_trader.features.indicators import relative_volume, rsi, sma
from egx_trader.strategies.base import Intent, Signal


@dataclass(frozen=True, slots=True)
class MeanReversionParams:
    rsi_period: int = 14
    ma_short: int = 20
    ma_long: int = 50
    hold_bars: int = 20
    """The original emits advice, not positions. Something has to close the trade
    for a comparison to be possible, so a fixed hold is used and named."""


class MeanReversionBaseline:
    name = "mean_reversion_baseline"

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self.p = params or MeanReversionParams()
        self._rsi: list[float | None] = []
        self._ma_s: list[float | None] = []
        self._ma_l: list[float | None] = []
        self._rvol: list[float | None] = []

    def prepare(self, series: OHLCVSeries) -> None:
        self._rsi = rsi(series, self.p.rsi_period)
        closes = series.closes
        self._ma_s = sma(closes, self.p.ma_short)
        self._ma_l = sma(closes, self.p.ma_long)
        self._rvol = relative_volume(series)

    def evaluate(
        self, series: OHLCVSeries, i: int, *, in_position: bool, entry_index: int | None
    ) -> Intent:
        bar = series.candles[i]

        if in_position and entry_index is not None:
            if i - entry_index >= self.p.hold_bars:
                return Intent(Signal.EXIT, bar.date, f"held {self.p.hold_bars} bars")
            return Intent(Signal.HOLD, bar.date, "holding")

        value = self._rsi[i]
        ma_s, ma_l = self._ma_s[i], self._ma_l[i]
        rvol = self._rvol[i]
        if None in (value, ma_s, ma_l, rvol):
            return Intent(Signal.HOLD, bar.date, "warming up")
        assert value is not None and ma_s is not None and ma_l is not None and rvol is not None

        vs_short = (bar.close - ma_s) / ma_s * 100
        vs_long = (bar.close - ma_l) / ma_l * 100
        features = {"rsi": value, "vs_ma20": vs_short, "vs_ma50": vs_long, "vol_ratio": rvol}

        bullish = vs_short >= 0 and vs_long >= 0
        high_volume = rvol >= 1.5

        # The original cascade, unchanged. Note both BUY branches require a LOW
        # RSI: this is why a breakout can never trigger it.
        if value <= 30 and bullish and high_volume:
            return Intent(
                Signal.ENTER, bar.date, "rsi<=30, bullish, high volume", features=features
            )
        if value <= 40 and vs_short >= 0:
            return Intent(Signal.ENTER, bar.date, "rsi<=40 above ma20", features=features)
        return Intent(Signal.HOLD, bar.date, f"no signal (rsi {value:.0f})", features=features)
