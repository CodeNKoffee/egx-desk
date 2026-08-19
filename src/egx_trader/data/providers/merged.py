"""Combine several providers into one series, recording where each bar came from.

Order is priority: the first provider with a bar for a date wins, and later
providers only fill dates the earlier ones lack. That makes `yahoo,eodhd` mean
"Yahoo, with its gaps patched from EODHD" rather than an unpredictable blend.

Provenance is not optional bookkeeping. Two feeds disagree on prices — Yahoo had
BIOC at 521.01 for 2026-08-13 while ThndrX showed 507.00 — so a backtest that
cannot say which vendor supplied a bar cannot distinguish a real edge from one
vendor's artefact.
"""

from __future__ import annotations

import datetime as dt

from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.data.providers.base import (
    DailyBarProvider,
    MergedResult,
    ProviderError,
)


def merge_providers(
    symbol: str,
    providers: list[DailyBarProvider],
    *,
    require_any: bool = True,
) -> MergedResult:
    """Fetch from each provider in priority order and stitch the results together."""
    chosen: dict[dt.date, Candle] = {}
    sources: dict[dt.date, str] = {}
    contributions: dict[str, int] = {}
    failures: dict[str, str] = {}

    for provider in providers:
        if not provider.is_configured():
            failures[provider.name] = "not configured"
            continue
        try:
            result = provider.fetch_daily(symbol)
        except ProviderError as exc:
            failures[provider.name] = str(exc)
            continue

        added = 0
        for candle in result.series.candles:
            if candle.date in chosen:
                continue
            chosen[candle.date] = candle
            sources[candle.date] = provider.name
            added += 1
        if added:
            contributions[provider.name] = added

    if not chosen and require_any:
        detail = "; ".join(f"{name}: {why}" for name, why in failures.items()) or "no providers"
        raise ProviderError(f"{symbol}: no provider returned bars ({detail})")

    series = OHLCVSeries(
        symbol=symbol,
        candles=tuple(chosen[day] for day in sorted(chosen)),
    )
    return MergedResult(
        series=series,
        sources=sources,
        contributions=contributions,
        failures=failures,
    )
