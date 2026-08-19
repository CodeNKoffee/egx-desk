"""Yahoo chart client, routed through a Cloudflare Worker proxy.

The Worker exists because Yahoo 429s datacenter and shared-residential egress.
It only proxies `/v8/finance/chart/` and requires the `x-proxy-key` shared secret.

Two silent-downgrade traps, both verified against the live feed:

1. **Intraday does not exist for `.CA`.** Yahoo downgrades any intraday `interval`
   to `1d` and advertises no intraday range, so asking for `5m` returns daily bars
   while looking like it worked. `fetch_daily` therefore exposes no interval knob —
   intraday has to be recorded, not fetched.

2. **`range=max` returns MONTHLY bars**, regardless of `interval=1d`. Measured on
   BIOC.CA: `10y` -> 2089 bars at a 1-day median spacing; `max` -> 275 bars at a
   31-day median spacing, every one a month-end. Backtesting on those unnoticed
   would produce fiction. `YEAR_10` is the deepest range that stays daily, and it
   is the default here; `fetch_daily` measures the spacing it actually got and
   refuses anything coarser than daily.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from egx_trader.data.models import Candle, Dividend, OHLCVSeries, Split
from egx_trader.market_calendar import EGXCalendar
from egx_trader.universe.models import normalize_symbol

_CHART_PATH: Final = "/v8/finance/chart/"
_DEFAULT_TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)

# Daily EGX bars sit 1 day apart, with 3-day gaps across the Fri/Sat weekend, so
# the median spacing is 1. Weekly data medians at 7, monthly at ~31. Four is a
# comfortable line between "daily" and "something coarser".
_MAX_DAILY_MEDIAN_GAP_DAYS: Final = 4


class ChartRange(StrEnum):
    """Ranges Yahoo advertises for `.CA` symbols. Note the absence of `1d`/`5d`."""

    MONTH_1 = "1mo"
    MONTH_3 = "3mo"
    MONTH_6 = "6mo"
    YTD = "ytd"
    YEAR_1 = "1y"
    YEAR_2 = "2y"
    YEAR_5 = "5y"
    YEAR_10 = "10y"
    """Deepest range that still returns daily bars. Use this for backtests."""

    MAX = "max"
    """Returns MONTHLY bars despite `interval=1d`. `fetch_daily` rejects it."""


# ── Errors ───────────────────────────────────────────────────────────────────


class MarketDataError(RuntimeError):
    """Base for anything that stopped us getting bars."""


class TransientUpstreamError(MarketDataError):
    """Network failure, 5xx, or 429. Worth retrying."""


class ProxyAuthError(MarketDataError):
    """The Worker rejected our `x-proxy-key`. Retrying will not help."""


class SymbolNotFoundError(MarketDataError):
    """Yahoo has no chart for this symbol. Retrying will not help."""


class InsufficientDataError(MarketDataError):
    """Fewer usable bars than the caller requires."""


class GranularityError(MarketDataError):
    """Upstream returned bars coarser than daily while claiming `interval=1d`.

    Happens with `range=max`. Loud failure beats backtesting on monthly bars that
    look daily.
    """


def _median_gap_days(series: OHLCVSeries) -> float | None:
    """Median calendar-day spacing between consecutive bars. None if too short."""
    dates = [c.date for c in series.candles]
    if len(dates) < 3:
        return None
    gaps = sorted((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1))
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return float(gaps[middle])
    return (gaps[middle - 1] + gaps[middle]) / 2


@dataclass(frozen=True, slots=True)
class FetchResult:
    series: OHLCVSeries
    raw_bar_count: int
    """Bars Yahoo returned, before dropping anything."""

    dropped_dates: tuple[dt.date, ...] = ()
    """Bars discarded for null or self-contradictory OHLCV."""

    synthetic_dates: tuple[dt.date, ...] = ()
    """Placeholder bars: flat OHLC with zero volume. Not sessions."""

    @property
    def dropped_bars(self) -> int:
        return len(self.dropped_dates)

    @property
    def dropped_fraction(self) -> float:
        """Raw drop rate. For EGX this overstates badly — see `egx_drop_fraction`."""
        return self.dropped_bars / self.raw_bar_count if self.raw_bar_count else 0.0

    def egx_drop_fraction(self, calendar: EGXCalendar) -> float:
        """Drop rate counting only bars that fall on real EGX sessions.

        Yahoo emits a Mon-Fri calendar while EGX trades Sun-Thu, so roughly a fifth
        of its rows are Fridays the exchange never opened. Measured across BIOC,
        AMOC, AFMC, ADIB and JUFO, the raw drop rate sits at 22-32% purely from
        that mismatch. Judging feed quality on the raw number would blacklist every
        symbol on the exchange.
        """
        real_drops = [d for d in self.dropped_dates if calendar.is_trading_day(d)]
        expected = len(self.series) + len(real_drops)
        return len(real_drops) / expected if expected else 0.0

    @property
    def median_gap_days(self) -> float | None:
        return _median_gap_days(self.series)

    @property
    def looks_daily(self) -> bool:
        gap = self.median_gap_days
        return gap is None or gap <= _MAX_DAILY_MEDIAN_GAP_DAYS


def _as_date(epoch_seconds: int | float) -> dt.date:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.UTC).date()


def _is_placeholder(candle: Candle) -> bool:
    """Flat OHLC with no volume — a carried-forward price, not a session."""
    return candle.volume == 0 and candle.open == candle.high == candle.low == candle.close


def _parse_events(events: dict[str, Any]) -> tuple[tuple[Split, ...], tuple[Dividend, ...]]:
    splits = tuple(
        Split(
            date=_as_date(entry["date"]),
            numerator=float(entry["numerator"]),
            denominator=float(entry["denominator"]),
        )
        for entry in (events.get("splits") or {}).values()
    )
    dividends = tuple(
        Dividend(date=_as_date(entry["date"]), amount=float(entry["amount"]))
        for entry in (events.get("dividends") or {}).values()
    )
    return (
        tuple(sorted(splits, key=lambda s: s.date)),
        tuple(sorted(dividends, key=lambda d: d.date)),
    )


def parse_chart(symbol: str, payload: dict[str, Any]) -> FetchResult:
    """Turn a Yahoo chart payload into a validated series.

    Bars with any null OHLCV field are dropped rather than patched. Yahoo's
    trailing bar for `.CA` routinely arrives with `close: null` during and after
    the session; inventing a value there would feed a fake price to the strategy.
    """
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise SymbolNotFoundError(f"{symbol}: {chart['error']}")

    results = chart.get("result") or []
    if not results:
        raise SymbolNotFoundError(f"{symbol}: chart response contained no result")

    result = results[0]
    meta = result.get("meta") or {}
    timestamps: list[int] = result.get("timestamp") or []

    indicators = result.get("indicators") or {}
    quote_blocks = indicators.get("quote") or [{}]
    quote = quote_blocks[0] if quote_blocks else {}
    adj_blocks = indicators.get("adjclose") or []
    adj_closes = (adj_blocks[0].get("adjclose") if adj_blocks else None) or []

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    candles: list[Candle] = []
    dropped_dates: list[dt.date] = []
    synthetic_dates: list[dt.date] = []

    for index, ts in enumerate(timestamps):

        def at(series: list[Any], i: int = index) -> Any:
            return series[i] if i < len(series) else None

        bar_date = _as_date(ts)
        o, h, low, c = at(opens), at(highs), at(lows), at(closes)
        if None in (o, h, low, c):
            dropped_dates.append(bar_date)
            continue

        volume = at(volumes)
        volume_int = int(volume) if volume is not None else 0
        adj = at(adj_closes)
        try:
            candles.append(
                Candle(
                    date=bar_date,
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=volume_int,
                    adj_close=float(adj) if adj is not None else None,
                )
            )
        except ValueError:
            # Fails Candle's own ordering checks (high < low, close outside range).
            # Feed corruption, not a bar — drop it and let quality gates see the rate.
            dropped_dates.append(bar_date)

    # Yahoo pads `.CA` series with a placeholder trailing bar: flat OHLC, zero
    # volume, carrying a stale price that disagrees with the broker's. Observed on
    # every EGX symbol tested (AFMC 239.00, AMOC 10.30, ADIB 54.85). It must not
    # become "the latest price" in a signal.
    #
    # Only TRAILING placeholders are trimmed. Mid-series flat zero-volume bars are
    # genuine no-trade sessions — common on thin EGX names — and dropping those
    # would compress the time axis, so a "55-day" channel would silently span far
    # more than 55 sessions. Thin names are excluded by the liquidity screen, not here.
    while candles and _is_placeholder(candles[-1]):
        synthetic_dates.append(candles.pop().date)
    synthetic_dates.reverse()

    splits, dividends = _parse_events(result.get("events") or {})

    market_time = meta.get("regularMarketTime")
    series = OHLCVSeries(
        symbol=symbol,
        currency=meta.get("currency") or "EGP",
        candles=tuple(candles),
        splits=splits,
        dividends=dividends,
        instrument_type=meta.get("instrumentType"),
        exchange_timestamp=(
            dt.datetime.fromtimestamp(market_time, tz=dt.UTC) if market_time else None
        ),
    )
    return FetchResult(
        series=series,
        raw_bar_count=len(timestamps),
        dropped_dates=tuple(dropped_dates),
        synthetic_dates=tuple(synthetic_dates),
    )


class YahooClient:
    """Fetches daily OHLCV through the Cloudflare Worker."""

    def __init__(
        self,
        base_url: str,
        proxy_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        if not proxy_key:
            raise ProxyAuthError(
                "No proxy key configured. The Worker rejects unkeyed requests with 403."
            )
        self._base_url = base_url.rstrip("/")
        self._proxy_key = proxy_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def __enter__(self) -> YahooClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @retry(
        retry=retry_if_exception_type(TransientUpstreamError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        reraise=True,
    )
    def _get(self, symbol: str, chart_range: ChartRange) -> dict[str, Any]:
        url = f"{self._base_url}{_CHART_PATH}{symbol}"
        params = {
            "range": chart_range.value,
            "interval": "1d",
            "includePrePost": "false",
            "events": "div,splits",
        }
        try:
            response = self._client.get(
                url, params=params, headers={"x-proxy-key": self._proxy_key}
            )
        except httpx.RequestError as exc:
            raise TransientUpstreamError(f"{symbol}: could not reach the proxy ({exc})") from exc

        if response.status_code == 403:
            raise ProxyAuthError(
                f"{symbol}: the Worker rejected x-proxy-key (403). "
                "Check EGX_PROXY_KEY matches the Worker's PROXY_KEY secret."
            )
        if response.status_code == 404:
            raise SymbolNotFoundError(f"{symbol}: upstream returned 404")
        if response.status_code == 429:
            raise TransientUpstreamError(
                f"{symbol}: rate limited (429) even through the Worker — back off"
            )
        if response.status_code >= 500:
            raise TransientUpstreamError(f"{symbol}: upstream {response.status_code}")
        if response.status_code != 200:
            raise MarketDataError(f"{symbol}: unexpected status {response.status_code}")

        payload: dict[str, Any] = response.json()
        return payload

    def fetch_daily(
        self,
        symbol: str,
        chart_range: ChartRange = ChartRange.YEAR_10,
        *,
        min_bars: int = 0,
        allow_non_daily: bool = False,
    ) -> FetchResult:
        """Daily OHLCV for one symbol.

        `min_bars` guards callers that need enough history to compute an indicator
        (MA50 needs 50+, the breakout strategy's 55-day Donchian needs 55+).

        Raises `GranularityError` if the bars that came back are coarser than daily,
        which is what `range=max` does. Pass `allow_non_daily=True` only if you
        genuinely want the coarser series and know it is not daily.

        The symbol is normalized first: upstream 404s on a bare `BIOC` and only
        answers to the Reuters-suffixed `BIOC.CA`.
        """
        symbol = normalize_symbol(symbol)
        result = parse_chart(symbol, self._get(symbol, chart_range))

        if not result.series.candles:
            raise InsufficientDataError(
                f"{symbol}: upstream returned {result.raw_bar_count} rows but no usable "
                f"bars ({result.dropped_bars} dropped, "
                f"{len(result.synthetic_dates)} placeholders)"
            )

        if not allow_non_daily and not result.looks_daily:
            raise GranularityError(
                f"{symbol}: range={chart_range.value} returned bars a median "
                f"{result.median_gap_days:.0f} days apart — that is not daily data, "
                f"despite interval=1d. Use ChartRange.YEAR_10 for deep daily history."
            )

        if len(result.series) < min_bars:
            raise InsufficientDataError(
                f"{symbol}: {len(result.series)} usable bars, need {min_bars} "
                f"(Yahoo returned {result.raw_bar_count}, {result.dropped_bars} dropped)"
            )
        return result
