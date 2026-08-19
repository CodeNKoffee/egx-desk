"""Daily-bar providers, selectable by config."""

from __future__ import annotations

from egx_trader.config import Settings
from egx_trader.data.providers.base import (
    DailyBarProvider,
    MergedResult,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderResult,
)
from egx_trader.data.providers.eodhd import EODHDProvider
from egx_trader.data.providers.merged import merge_providers
from egx_trader.data.providers.tv_csv import TradingViewCSVProvider
from egx_trader.data.providers.yahoo_provider import YahooProvider

__all__ = [
    "DailyBarProvider",
    "EODHDProvider",
    "MergedResult",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ProviderResult",
    "TradingViewCSVProvider",
    "YahooProvider",
    "build_providers",
    "merge_providers",
]


def build_providers(settings: Settings) -> list[DailyBarProvider]:
    """Instantiate the providers named in `EGX_DATA_PROVIDERS`, in that order.

    Unknown names raise rather than being skipped: silently ignoring a typo would
    mean quietly trading on a different data source than the one configured.
    """
    built: list[DailyBarProvider] = []
    for raw in settings.data_providers.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name == "yahoo":
            built.append(YahooProvider(settings.yahoo_base_url, settings.proxy_key))
        elif name == "eodhd":
            built.append(EODHDProvider(settings.eodhd_api_key))
        elif name in ("tradingview_csv", "tradingview", "csv"):
            built.append(TradingViewCSVProvider(settings.tv_csv_dir))
        else:
            raise ValueError(
                f"unknown provider {name!r} in EGX_DATA_PROVIDERS. "
                "Valid: yahoo, eodhd, tradingview_csv"
            )
    if not built:
        raise ValueError("EGX_DATA_PROVIDERS is empty — no data source configured")
    return built
