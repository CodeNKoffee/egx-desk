"""Indicator tests.

RSI and the true-range family are corrections to what the existing stack computes,
so they get checked against reference behaviour rather than against themselves.
"""

from __future__ import annotations

import datetime as dt

import pytest

from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.features.indicators import (
    atr,
    chandelier_stop,
    donchian,
    relative_volume,
    rsi,
    sma,
    true_range,
    turnover_egp,
)

START = dt.date(2026, 1, 4)


def make(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[int] | None = None,
) -> OHLCVSeries:
    candles = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * 1.01
        low = lows[i] if lows else c * 0.99
        candles.append(
            Candle(
                date=START + dt.timedelta(days=i),
                open=c,
                high=max(h, c),
                low=min(low, c),
                close=c,
                volume=volumes[i] if volumes else 1000,
            )
        )
    return OHLCVSeries(symbol="TEST.CA", candles=tuple(candles))


class TestRSI:
    def test_none_until_there_is_history(self) -> None:
        values = rsi(make([100.0] * 20), period=14)
        assert all(v is None for v in values[:14])
        assert values[14] is not None

    def test_a_pure_uptrend_pins_at_100(self) -> None:
        """No down closes means no average loss. That is RSI 100 by definition,
        not a divide-by-zero."""
        values = rsi(make([100 + i for i in range(30)]), period=14)
        assert values[-1] == 100.0

    def test_a_pure_downtrend_pins_at_0(self) -> None:
        values = rsi(make([100 - i for i in range(30)]), period=14)
        assert values[-1] == pytest.approx(0.0, abs=1e-6)

    def test_wilder_differs_from_a_simple_average_after_a_regime_change(self) -> None:
        """Both existing implementations average gains and losses with an SMA,
        which is a different indicator, and the gap is not academic.

        Twenty declining bars followed by sixteen rising ones: the SMA's 14-bar
        window has forgotten the decline entirely and reads 100 — pinned at the
        overbought extreme — while Wilder still carries it and reads about 69. A
        rule that sells above 70 fires on one and not the other.
        """
        closes = [100.0]
        for _ in range(20):
            closes.append(closes[-1] * 0.985)
        for _ in range(16):
            closes.append(closes[-1] * 1.015)

        wilder = rsi(make(closes), period=14)[-1]

        gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
        sma_gain = sum(gains[-14:]) / 14
        sma_loss = sum(losses[-14:]) / 14
        naive = 100.0 if sma_loss == 0 else 100 - 100 / (1 + sma_gain / sma_loss)

        assert wilder is not None
        assert naive == pytest.approx(100.0), "the SMA window drops the decline"
        assert wilder < 80, "Wilder still carries it"
        assert abs(wilder - naive) > 20

    def test_stays_within_bounds(self) -> None:
        closes = [100.0]
        for i in range(60):
            closes.append(closes[-1] * (1.02 if i % 2 else 0.985))
        for v in rsi(make(closes)):
            if v is not None:
                assert 0.0 <= v <= 100.0


class TestTrueRange:
    def test_uses_the_gap_not_just_the_intraday_range(self) -> None:
        """A name that gaps up and never trades back has a tiny intraday range and
        an enormous true range. A stop sized on the former sits inside the noise."""
        series = make([100.0, 150.0], highs=[101.0, 151.0], lows=[99.0, 149.0])
        tr = true_range(series.candles)
        assert tr[1] == pytest.approx(51.0)  # high 151 - prev close 100
        assert tr[1] > (151.0 - 149.0)

    def test_first_bar_has_none(self) -> None:
        assert true_range(make([100.0, 101.0]).candles)[0] is None


class TestATR:
    def test_none_until_enough_history(self) -> None:
        values = atr(make([100.0 + i for i in range(20)]), period=14)
        assert values[0] is None
        assert values[-1] is not None

    def test_widens_with_volatility(self) -> None:
        calm = atr(make([100.0 + i * 0.1 for i in range(40)]), period=14)[-1]
        wild = atr(
            make([100.0 + (i % 2) * 20 for i in range(40)]),
            period=14,
        )[-1]
        assert calm is not None and wild is not None
        assert wild > calm * 5


class TestDonchian:
    def test_excludes_the_current_bar(self) -> None:
        """A breakout must compare today against the PRIOR window. Including today
        would make the condition trivially true at every new high."""
        closes = [100.0] * 10 + [200.0]
        up, _ = donchian(make(closes, highs=closes), period=10)
        assert up[10] == 100.0, "the channel must not contain today's own high"

    def test_none_before_the_window_fills(self) -> None:
        up, down = donchian(make([100.0] * 20), period=10)
        assert up[9] is None
        assert down[10] is not None

    def test_tracks_the_extremes(self) -> None:
        highs = [10, 20, 15, 30, 12, 18, 11, 14, 16, 13, 100]
        lows = [5, 8, 6, 9, 4, 7, 3, 6, 5, 8, 50]
        up, down = donchian(
            make(
                [float(h) for h in highs],
                highs=[float(h) for h in highs],
                lows=[float(x) for x in lows],
            ),
            period=10,
        )
        assert up[10] == 30.0
        assert down[10] == 3.0


class TestRelativeVolume:
    def test_excludes_the_current_bar_from_its_own_baseline(self) -> None:
        """Otherwise a huge day inflates the average it is measured against."""
        volumes = [1000] * 20 + [5000]
        values = relative_volume(make([100.0] * 21, volumes=volumes), period=20)
        assert values[20] == pytest.approx(5.0)

    def test_flat_volume_is_one(self) -> None:
        values = relative_volume(make([100.0] * 25, volumes=[1000] * 25), period=20)
        assert values[-1] == pytest.approx(1.0)


class TestTurnover:
    def test_measures_value_not_share_count(self) -> None:
        """A million shares at 2 EGP and a thousand at 500 EGP are not comparable
        positions, which is why the liquidity screen uses value."""
        cheap = turnover_egp(make([2.0] * 25, volumes=[1_000_000] * 25), period=20)[-1]
        pricey = turnover_egp(make([500.0] * 25, volumes=[1_000] * 25), period=20)[-1]
        assert cheap == pytest.approx(2_000_000)
        assert pricey == pytest.approx(500_000)


class TestChandelierStop:
    def test_ratchets_up_with_new_highs(self) -> None:
        """The exit that decides whether a system captures a large move or clips it."""
        closes = [100.0 + i * 5 for i in range(40)]
        series = make(closes, highs=closes)
        atrs = atr(series, period=14)
        early = chandelier_stop(series, 20, 25, atr_values=atrs)
        late = chandelier_stop(series, 20, 39, atr_values=atrs)
        assert early is not None and late is not None
        assert late > early, "the stop must only ever ratchet up"

    def test_sits_below_the_running_high(self) -> None:
        closes = [100.0 + i * 5 for i in range(40)]
        series = make(closes, highs=closes)
        stop = chandelier_stop(series, 20, 39, atr_values=atr(series, period=14))
        assert stop is not None
        assert stop < max(series.highs[20:40])

    def test_none_without_atr(self) -> None:
        series = make([100.0] * 5)
        assert chandelier_stop(series, 0, 4, atr_values=[None] * 5) is None


class TestSMA:
    def test_matches_a_hand_computation(self) -> None:
        assert sma([1.0, 2.0, 3.0, 4.0], 2) == [None, 1.5, 2.5, 3.5]
