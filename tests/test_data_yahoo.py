"""Yahoo client and chart-parsing tests. No network — payloads are synthesised."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import httpx
import pytest
import respx

from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.data.yahoo import (
    ChartRange,
    FetchResult,
    GranularityError,
    InsufficientDataError,
    MarketDataError,
    ProxyAuthError,
    SymbolNotFoundError,
    TransientUpstreamError,
    YahooClient,
    parse_chart,
)
from egx_trader.market_calendar import EGXCalendar

BASE = "https://worker.example"
KEY = "test-proxy-key"


def epoch(year: int, month: int, day: int) -> int:
    return int(dt.datetime(year, month, day, 12, 0, tzinfo=dt.UTC).timestamp())


def chart_payload(
    *,
    timestamps: list[int],
    opens: list[Any],
    highs: list[Any],
    lows: list[Any],
    closes: list[Any],
    volumes: list[Any] | None = None,
    splits: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"currency": "EGP", **(meta or {})},
                    "timestamp": timestamps,
                    "events": {"splits": splits or {}},
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes if volumes is not None else [100] * len(closes),
                            }
                        ]
                    },
                }
            ],
        }
    }


SIMPLE = chart_payload(
    timestamps=[epoch(2026, 8, 16), epoch(2026, 8, 17)],
    opens=[10.0, 11.0],
    highs=[11.0, 12.0],
    lows=[9.5, 10.5],
    closes=[10.5, 11.5],
    volumes=[1000, 2000],
)


def _run(dates: list[dt.date]) -> dict[str, Any]:
    closes = [100.0 * (1.001**i) for i in range(len(dates))]
    return chart_payload(
        timestamps=[
            int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.UTC).timestamp()) for d in dates
        ],
        opens=closes,
        highs=[c * 1.01 for c in closes],
        lows=[c * 0.99 for c in closes],
        closes=closes,
    )


def _egx_weekdays(start: dt.date, count: int) -> list[dt.date]:
    """Consecutive Sun-Thu sessions, i.e. real EGX daily spacing."""
    days: list[dt.date] = []
    day = start
    while len(days) < count:
        if day.weekday() in {6, 0, 1, 2, 3}:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


DAILY_RUN = _run(_egx_weekdays(dt.date(2026, 1, 4), 40))
MONTHLY_RUN = _run([dt.date(2024, m, 28) for m in range(1, 13)])


# ── Parsing ──────────────────────────────────────────────────────────────────


class TestParsing:
    def test_parses_full_ohlcv(self) -> None:
        result = parse_chart("BIOC.CA", SIMPLE)
        assert len(result.series) == 2
        first = result.series.candles[0]
        assert (first.open, first.high, first.low, first.close) == (10.0, 11.0, 9.5, 10.5)
        assert first.volume == 1000

    def test_keeps_high_and_low_unlike_egx_api(self) -> None:
        """A prior implementation discarded open/high/low; without them there is no
        ATR and no Donchian channel."""
        series = parse_chart("BIOC.CA", SIMPLE).series
        assert series.highs == [11.0, 12.0]
        assert series.lows == [9.5, 10.5]

    def test_drops_bars_with_null_close(self) -> None:
        """Yahoo's trailing `.CA` bar routinely has close: null. Dropping beats patching."""
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 16), epoch(2026, 8, 17)],
            opens=[10.0, 11.0],
            highs=[11.0, 12.0],
            lows=[9.5, 10.5],
            closes=[10.5, None],
        )
        result = parse_chart("BIOC.CA", payload)
        assert len(result.series) == 1
        assert result.dropped_bars == 1
        assert result.raw_bar_count == 2
        assert result.dropped_fraction == 0.5

    def test_drops_bars_that_violate_ohlc_ordering(self) -> None:
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 16), epoch(2026, 8, 17)],
            opens=[10.0, 11.0],
            highs=[11.0, 9.0],  # high below low — corrupt
            lows=[9.5, 10.5],
            closes=[10.5, 11.0],
        )
        result = parse_chart("BIOC.CA", payload)
        assert len(result.series) == 1
        assert result.dropped_bars == 1

    def test_missing_volume_becomes_zero_not_dropped(self) -> None:
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 17)],
            opens=[10.0],
            highs=[11.0],
            lows=[9.5],
            closes=[10.5],
            volumes=[None],
        )
        result = parse_chart("BIOC.CA", payload)
        assert result.series.candles[0].volume == 0
        assert result.dropped_bars == 0

    def test_parses_splits(self) -> None:
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 17)],
            opens=[10.0],
            highs=[11.0],
            lows=[9.5],
            closes=[10.5],
            splits={
                "1755000000": {
                    "date": epoch(2026, 8, 17),
                    "numerator": 2.0,
                    "denominator": 1.0,
                }
            },
        )
        series = parse_chart("BIOC.CA", payload).series
        assert len(series.splits) == 1
        assert series.splits[0].ratio == 2.0

    def test_records_instrument_type_and_market_time(self) -> None:
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 17)],
            opens=[10.0],
            highs=[11.0],
            lows=[9.5],
            closes=[10.5],
            meta={"instrumentType": "MUTUALFUND", "regularMarketTime": epoch(2024, 7, 23)},
        )
        series = parse_chart("BIOC.CA", payload).series
        assert series.instrument_type == "MUTUALFUND"
        assert series.exchange_timestamp is not None
        assert series.exchange_timestamp.date() == dt.date(2024, 7, 23)

    def test_chart_error_raises_symbol_not_found(self) -> None:
        with pytest.raises(SymbolNotFoundError):
            parse_chart("NOPE.CA", {"chart": {"error": {"code": "Not Found"}, "result": None}})

    def test_empty_result_raises(self) -> None:
        with pytest.raises(SymbolNotFoundError):
            parse_chart("NOPE.CA", {"chart": {"error": None, "result": []}})


# ── Series invariants ────────────────────────────────────────────────────────


class TestSeriesInvariants:
    def test_rejects_unordered_candles(self) -> None:
        late = Candle(date=dt.date(2026, 8, 17), open=1, high=2, low=1, close=1.5, volume=1)
        early = Candle(date=dt.date(2026, 8, 16), open=1, high=2, low=1, close=1.5, volume=1)
        with pytest.raises(ValueError, match="ascending"):
            OHLCVSeries(symbol="X.CA", candles=(late, early))

    def test_rejects_duplicate_dates(self) -> None:
        bar = Candle(date=dt.date(2026, 8, 17), open=1, high=2, low=1, close=1.5, volume=1)
        with pytest.raises(ValueError, match="duplicate"):
            OHLCVSeries(symbol="X.CA", candles=(bar, bar))

    def test_turnover_is_price_times_volume(self) -> None:
        bar = Candle(
            date=dt.date(2026, 8, 17), open=495, high=507, low=490, close=500.0, volume=1000
        )
        assert bar.turnover == 500_000.0

    def test_candle_rejects_close_outside_the_bar_range(self) -> None:
        with pytest.raises(ValueError, match=re.escape("close 500.0 outside")):
            Candle(date=dt.date(2026, 8, 17), open=1, high=2, low=1, close=500.0, volume=1000)

    def test_candle_rejects_high_below_low(self) -> None:
        with pytest.raises(ValueError, match=re.escape("high 1.0 < low 2.0")):
            Candle(date=dt.date(2026, 8, 17), open=1.5, high=1, low=2, close=1.5, volume=1)


# ── HTTP behaviour ───────────────────────────────────────────────────────────


class TestHttp:
    def test_sends_the_proxy_key_and_daily_interval(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/v8/finance/chart/BIOC.CA").respond(json=SIMPLE)
            with YahooClient(BASE, KEY) as client:
                client.fetch_daily("BIOC.CA", ChartRange.MAX)

            request = route.calls[0].request
            assert request.headers["x-proxy-key"] == KEY
            assert request.url.params["interval"] == "1d"
            assert request.url.params["range"] == "max"
            assert request.url.params["events"] == "div,splits"

    def test_403_is_a_proxy_auth_error_and_is_not_retried(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/v8/finance/chart/BIOC.CA").respond(status_code=403)
            with YahooClient(BASE, KEY) as client, pytest.raises(ProxyAuthError, match="403"):
                client.fetch_daily("BIOC.CA")
            assert route.call_count == 1, "auth failures must not be retried"

    def test_404_is_not_retried(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/v8/finance/chart/NOPE.CA").respond(status_code=404)
            with YahooClient(BASE, KEY) as client, pytest.raises(SymbolNotFoundError):
                client.fetch_daily("NOPE.CA")
            assert route.call_count == 1

    def test_429_is_retried_then_reraised(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/v8/finance/chart/BIOC.CA").respond(status_code=429)
            with YahooClient(BASE, KEY) as client, pytest.raises(TransientUpstreamError):
                client.fetch_daily("BIOC.CA")
            assert route.call_count == 4, "transient failures should exhaust the retry budget"

    def test_retry_recovers_after_a_transient_failure(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/BIOC.CA").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json=SIMPLE),
                ]
            )
            with YahooClient(BASE, KEY) as client:
                assert len(client.fetch_daily("BIOC.CA").series) == 2

    def test_network_error_is_transient(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/BIOC.CA").mock(side_effect=httpx.ConnectError("boom"))
            with YahooClient(BASE, KEY) as client, pytest.raises(TransientUpstreamError):
                client.fetch_daily("BIOC.CA")

    def test_unexpected_status_raises(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/BIOC.CA").respond(status_code=418)
            with YahooClient(BASE, KEY) as client, pytest.raises(MarketDataError, match="418"):
                client.fetch_daily("BIOC.CA")

    def test_min_bars_is_enforced(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/BIOC.CA").respond(json=SIMPLE)
            with (
                YahooClient(BASE, KEY) as client,
                pytest.raises(InsufficientDataError, match="need 55"),
            ):
                client.fetch_daily("BIOC.CA", min_bars=55)

    def test_empty_key_refuses_to_construct(self) -> None:
        with pytest.raises(ProxyAuthError, match="No proxy key"):
            YahooClient(BASE, "")

    def test_defaults_to_the_deepest_daily_range(self) -> None:
        """`max` returns monthly bars, so it must not be the default."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/v8/finance/chart/BIOC.CA").respond(json=DAILY_RUN)
            with YahooClient(BASE, KEY) as client:
                client.fetch_daily("BIOC.CA")
            assert route.calls[0].request.url.params["range"] == "10y"


# ── Granularity guard ────────────────────────────────────────────────────────


class TestGranularityGuard:
    """`range=max` silently returns monthly bars. Verified live on BIOC.CA:
    10y -> 2089 bars at 1-day median spacing; max -> 275 at 31-day spacing."""

    def test_monthly_bars_are_rejected(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/AMOC.CA").respond(json=MONTHLY_RUN)
            with YahooClient(BASE, KEY) as client, pytest.raises(GranularityError) as excinfo:
                client.fetch_daily("AMOC.CA", ChartRange.MAX)
            assert "not daily data" in str(excinfo.value)
            assert "YEAR_10" in str(excinfo.value)

    def test_daily_bars_pass(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/BIOC.CA").respond(json=DAILY_RUN)
            with YahooClient(BASE, KEY) as client:
                assert client.fetch_daily("BIOC.CA", ChartRange.YEAR_10).looks_daily

    def test_weekend_gaps_do_not_trip_the_guard(self) -> None:
        """EGX runs Sun-Thu, so a 3-day gap every week is normal daily data."""
        result = parse_chart("BIOC.CA", DAILY_RUN)
        assert result.median_gap_days == 1
        assert result.looks_daily

    def test_non_daily_can_be_opted_into(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/v8/finance/chart/AMOC.CA").respond(json=MONTHLY_RUN)
            with YahooClient(BASE, KEY) as client:
                result = client.fetch_daily("AMOC.CA", ChartRange.MAX, allow_non_daily=True)
            assert not result.looks_daily

    def test_short_series_cannot_be_classified(self) -> None:
        """Two bars is not enough to infer spacing — do not guess, do not block."""
        result = parse_chart("BIOC.CA", SIMPLE)
        assert result.median_gap_days is None
        assert result.looks_daily


# ── Synthetic placeholder bars ───────────────────────────────────────────────


class TestSyntheticBars:
    """Yahoo pads `.CA` series with a flat, zero-volume trailing bar. Observed on
    every EGX symbol tested (AFMC 239.00, AMOC 10.30, ADIB 54.85) — and those
    prices disagree with the broker's, so they must never reach an indicator."""

    def test_trailing_flat_zero_volume_bar_is_trimmed(self) -> None:
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 16), epoch(2026, 8, 17)],
            opens=[10.0, 239.0],
            highs=[11.0, 239.0],
            lows=[9.5, 239.0],
            closes=[10.5, 239.0],
            volumes=[1000, 0],
        )
        result = parse_chart("AFMC.CA", payload)
        assert len(result.series) == 1
        assert result.synthetic_dates == (dt.date(2026, 8, 17),)
        assert result.dropped_bars == 0, "placeholders are tracked apart from corrupt bars"

    def test_several_trailing_placeholders_are_all_trimmed(self) -> None:
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 16), epoch(2026, 8, 17), epoch(2026, 8, 18)],
            opens=[10.0, 239.0, 239.0],
            highs=[11.0, 239.0, 239.0],
            lows=[9.5, 239.0, 239.0],
            closes=[10.5, 239.0, 239.0],
            volumes=[1000, 0, 0],
        )
        result = parse_chart("AFMC.CA", payload)
        assert len(result.series) == 1
        assert len(result.synthetic_dates) == 2
        assert result.synthetic_dates == (dt.date(2026, 8, 17), dt.date(2026, 8, 18))

    def test_mid_series_no_trade_bars_are_kept(self) -> None:
        """Dropping these would compress the time axis, so a 55-day channel would
        silently span far more than 55 sessions. Thin names are the liquidity
        screen's problem, not the parser's."""
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 16), epoch(2026, 8, 17), epoch(2026, 8, 18)],
            opens=[10.0, 10.5, 11.0],
            highs=[11.0, 10.5, 12.0],
            lows=[9.5, 10.5, 10.5],
            closes=[10.5, 10.5, 11.5],
            volumes=[1000, 0, 2000],
        )
        result = parse_chart("BIOC.CA", payload)
        assert len(result.series) == 3
        assert result.synthetic_dates == ()

    def test_a_real_untraded_bar_with_a_range_is_kept(self) -> None:
        """Zero volume alone is not enough — an actual range means it printed."""
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 17)],
            opens=[10.0],
            highs=[11.0],
            lows=[9.5],
            closes=[10.5],
            volumes=[0],
        )
        assert len(parse_chart("X.CA", payload).series) == 1

    def test_a_flat_bar_that_actually_traded_is_kept(self) -> None:
        """Flat OHLC with real volume is a legitimate limit-locked session."""
        payload = chart_payload(
            timestamps=[epoch(2026, 8, 17)],
            opens=[10.0],
            highs=[10.0],
            lows=[10.0],
            closes=[10.0],
            volumes=[5000],
        )
        assert len(parse_chart("X.CA", payload).series) == 1


# ── EGX-adjusted drop rate ───────────────────────────────────────────────────


class TestEgxDropFraction:
    """Yahoo emits Mon-Fri; EGX trades Sun-Thu. Roughly a fifth of its rows are
    Fridays the exchange never opened, which pushed the raw drop rate to 22-32%
    on live data and would blacklist every symbol on the exchange."""

    def test_friday_nulls_do_not_count_against_the_feed(self) -> None:
        cal = EGXCalendar(strict=False)
        sessions = _egx_weekdays(dt.date(2026, 1, 4), 20)
        fridays = [dt.date(2026, 1, 9), dt.date(2026, 1, 16), dt.date(2026, 1, 23)]

        payload = _run(sessions)
        result = parse_chart("X.CA", payload)
        inflated = FetchResult(
            series=result.series,
            raw_bar_count=len(sessions) + len(fridays),
            dropped_dates=tuple(fridays),
        )

        assert inflated.dropped_fraction > 0.12
        assert inflated.egx_drop_fraction(cal) == 0.0

    def test_missing_real_sessions_still_count(self) -> None:
        cal = EGXCalendar(strict=False)
        sessions = _egx_weekdays(dt.date(2026, 1, 4), 20)
        missing = [d for d in _egx_weekdays(dt.date(2026, 2, 1), 5)]

        result = parse_chart("X.CA", _run(sessions))
        with_gaps = FetchResult(
            series=result.series,
            raw_bar_count=len(sessions) + len(missing),
            dropped_dates=tuple(missing),
        )
        assert with_gaps.egx_drop_fraction(cal) == pytest.approx(5 / 25)
