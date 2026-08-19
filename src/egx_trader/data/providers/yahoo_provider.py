"""Yahoo, wrapped as a provider.

Free and automatable, and the only source currently proven to work end to end.
It also loses 22-30% of EGX sessions at random, spread evenly across Sun-Thu, so
it is the baseline every other provider gets measured against rather than the
answer.
"""

from __future__ import annotations

from egx_trader.data.providers.base import ProviderError, ProviderResult
from egx_trader.data.yahoo import ChartRange, MarketDataError, YahooClient


class YahooProvider:
    name = "yahoo"

    def __init__(
        self,
        base_url: str,
        proxy_key: str,
        chart_range: ChartRange = ChartRange.YEAR_10,
    ) -> None:
        self._base_url = base_url
        self._proxy_key = proxy_key
        self._range = chart_range

    def is_configured(self) -> bool:
        return bool(self._proxy_key)

    def fetch_daily(self, symbol: str) -> ProviderResult:
        try:
            with YahooClient(self._base_url, self._proxy_key) as client:
                result = client.fetch_daily(symbol, self._range)
        except MarketDataError as exc:
            raise ProviderError(f"yahoo: {exc}") from exc

        return ProviderResult(
            series=result.series,
            provider=self.name,
            raw_bar_count=result.raw_bar_count,
            dropped_dates=result.dropped_dates,
            notes=(
                (f"trimmed {len(result.synthetic_dates)} placeholder bars",)
                if result.synthetic_dates
                else ()
            ),
        )
