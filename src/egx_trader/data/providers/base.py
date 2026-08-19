"""Pluggable daily-bar providers.

Yahoo is free and automatable but loses 22-30% of EGX sessions at random. EODHD
is API-native at $19.99/mo but its EGX coverage is unverified. TradingView has
real EGX data and an official CSV export, but no data API at any price and a ToS
that forbids automated collection outright — so it is a manual import, not a
client.

None of those is obviously right, and the only way to find out is to measure them
against each other on the same symbols. So the source is a config switch and
providers compose: `EGX_DATA_PROVIDERS=yahoo,eodhd` fills Yahoo's gaps from EODHD
and records which provider each bar came from.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from egx_trader.data.models import OHLCVSeries


class ProviderError(RuntimeError):
    """A provider could not supply data. Distinct from "supplied bad data"."""


class ProviderNotConfiguredError(ProviderError):
    """Missing credentials or input files. Retrying will not help."""


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Bars from one provider, plus what it had to discard getting there."""

    series: OHLCVSeries
    provider: str
    raw_bar_count: int = 0
    dropped_dates: tuple[dt.date, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def dates(self) -> set[dt.date]:
        return {c.date for c in self.series.candles}


@dataclass(frozen=True, slots=True)
class MergedResult:
    """Bars assembled from several providers, with per-bar provenance.

    `sources` is the point of this class: without knowing which provider supplied
    a given bar, a backtest cannot tell a real edge from one vendor's artefact.
    """

    series: OHLCVSeries
    sources: dict[dt.date, str] = field(default_factory=dict)
    contributions: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def provenance_summary(self) -> str:
        if not self.contributions:
            return "no bars"
        parts = [f"{name}={count}" for name, count in sorted(self.contributions.items())]
        return ", ".join(parts)


@runtime_checkable
class DailyBarProvider(Protocol):
    """Anything that can supply daily EGX bars for a symbol."""

    name: str

    def is_configured(self) -> bool:
        """False when credentials or inputs are missing, so callers can skip it."""
        ...

    def fetch_daily(self, symbol: str) -> ProviderResult:
        """Daily OHLCV, ascending. Raises ProviderError when unavailable."""
        ...
