"""Indicators, computed from full OHLCV.

Two of these are corrections rather than additions.

**RSI uses Wilder's smoothing**, not a simple moving average of gains and losses.
Both existing implementations in this workspace use an SMA, which is not RSI — it
reacts faster and overshoots, and every published threshold (30, 70) assumes
Wilder. A rule tuned against SMA-RSI is tuned against a different indicator.

**Highs and lows are real highs and lows.** The old `weekHigh`/`weekLow` took
max/min of *closes*, which understates range on exactly the volatile days that
matter. ATR and Donchian both depend on the true extremes.

Everything returns a list aligned to the input series, with `None` where there is
not yet enough history. Never zero, never forward-filled: a missing indicator is
missing, and a strategy must not be able to mistake it for a value.
"""

from __future__ import annotations

from egx_trader.data.models import Candle, OHLCVSeries


def _wilder_smooth(values: list[float], period: int) -> list[float | None]:
    """Wilder's smoothing: seed with a simple mean, then decay by 1/period.

    Equivalent to an EMA with alpha = 1/period, which is what "Wilder's" means
    and what RSI/ATR/ADX are all defined against.
    """
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(series: OHLCVSeries, period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    closes = series.closes
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]

    avg_gain = _wilder_smooth(gains, period)
    avg_loss = _wilder_smooth(losses, period)

    for i in range(len(gains)):
        g, loss = avg_gain[i], avg_loss[i]
        if g is None or loss is None:
            continue
        # A period with no down closes is RSI 100 by definition, not a divide by
        # zero. Guarding this is what stops a runaway name producing NaN.
        out[i + 1] = 100.0 if loss == 0 else 100 - (100 / (1 + g / loss))
    return out


def true_range(candles: tuple[Candle, ...]) -> list[float | None]:
    """max(high-low, |high-prev_close|, |low-prev_close|).

    The two gap terms are the point: a name that opens limit-up and never trades
    back has a tiny intraday range but an enormous true range, and a stop sized on
    the former would sit inside the noise.
    """
    out: list[float | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        out[i] = max(
            c.high - c.low,
            abs(c.high - prev.close),
            abs(c.low - prev.close),
        )
    return out


def atr(series: OHLCVSeries, period: int = 14) -> list[float | None]:
    """Average True Range, Wilder-smoothed. The unit the trailing stop is sized in."""
    tr = true_range(series.candles)
    usable = [v for v in tr if v is not None]
    out: list[float | None] = [None] * len(series)
    if len(usable) < period:
        return out
    smoothed = _wilder_smooth(usable, period)
    # `tr` has one leading None; realign.
    for i, value in enumerate(smoothed):
        out[i + 1] = value
    return out


def donchian(
    series: OHLCVSeries, period: int = 55
) -> tuple[list[float | None], list[float | None]]:
    """Rolling (highest high, lowest low) over `period` bars, EXCLUDING the current one.

    Excluding today is what makes a breakout meaningful: "today's close is above
    the highest high of the prior 55 sessions". Including today would make the
    condition trivially true at every new high, since today's high would be in the
    channel it is being compared against.
    """
    highs, lows = series.highs, series.lows
    up: list[float | None] = [None] * len(series)
    down: list[float | None] = [None] * len(series)
    for i in range(period, len(series)):
        window_h = highs[i - period : i]
        window_l = lows[i - period : i]
        up[i] = max(window_h)
        down[i] = min(window_l)
    return up, down


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def relative_volume(series: OHLCVSeries, period: int = 20) -> list[float | None]:
    """Volume as a multiple of its own recent average, excluding the current bar.

    A breakout without volume is a quote, not a move. Excluding the current bar
    keeps the comparison honest — otherwise a huge day inflates its own baseline.
    """
    volumes = [float(v) for v in series.volumes]
    out: list[float | None] = [None] * len(series)
    for i in range(period, len(series)):
        window = volumes[i - period : i]
        avg = sum(window) / period
        out[i] = volumes[i] / avg if avg > 0 else None
    return out


def turnover_egp(series: OHLCVSeries, period: int = 20) -> list[float | None]:
    """Average daily traded value. The liquidity screen runs on this, not on volume.

    Share count alone is meaningless across EGX: a million shares of a 2 EGP name
    and a thousand of a 500 EGP name are not comparable positions.
    """
    values = [c.close * c.volume for c in series.candles]
    return sma(values, period)


def chandelier_stop(
    series: OHLCVSeries,
    entry_index: int,
    current_index: int,
    *,
    atr_values: list[float | None],
    multiple: float = 3.0,
) -> float | None:
    """Highest high since entry, minus `multiple` x ATR.

    This is the exit that decides whether a system captures a large move or clips
    it. The stop only ever ratchets up, so a position is given room to breathe
    while trending and is closed once it stops making highs. A fixed profit target
    would have taken BIOC at +30% and missed everything after.
    """
    if current_index < entry_index or current_index >= len(series):
        return None
    current_atr = atr_values[current_index]
    if current_atr is None:
        return None
    highest = max(series.highs[entry_index : current_index + 1])
    return highest - multiple * current_atr
