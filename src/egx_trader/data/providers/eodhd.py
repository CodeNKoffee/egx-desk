"""EODHD provider.

$19.99/mo for All-World end-of-day, $29.99 with intraday. EGX symbols take the
`.EGX` suffix rather than Yahoo's `.CA`. Their demo token cannot reach EGX, so
coverage quality is unverified — that is exactly what `egx compare-providers`
exists to settle before anyone pays.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.data.providers.base import (
    ProviderError,
    ProviderNotConfiguredError,
    ProviderResult,
)
from egx_trader.universe.models import symbol_code

_BASE: Final = "https://eodhd.com/api"
_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)


class EODHDTransientError(ProviderError):
    """Network or 5xx. Worth retrying."""


class EODHDProvider:
    """Daily bars from EODHD."""

    name = "eodhd"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        exchange_suffix: str = "EGX",
    ) -> None:
        self._api_key = api_key
        self._suffix = exchange_suffix
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> EODHDProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _eodhd_symbol(self, symbol: str) -> str:
        """`BIOC.CA` and `BIOC` both become `BIOC.EGX`."""
        return f"{symbol_code(symbol)}.{self._suffix}"

    @retry(
        retry=retry_if_exception_type(EODHDTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        try:
            response = self._client.get(
                f"{_BASE}/{path}", params={**params, "api_token": self._api_key, "fmt": "json"}
            )
        except httpx.RequestError as exc:
            raise EODHDTransientError(f"could not reach EODHD ({exc})") from exc

        if response.status_code in (401, 403):
            raise ProviderNotConfiguredError(
                "EODHD rejected the API token (HTTP "
                f"{response.status_code}). EGX needs a paid plan; the demo token "
                "only covers a handful of US symbols."
            )
        if response.status_code == 404:
            raise ProviderError("EODHD has no data for that symbol")
        if response.status_code == 429:
            raise EODHDTransientError("EODHD rate limit hit")
        if response.status_code >= 500:
            raise EODHDTransientError(f"EODHD upstream {response.status_code}")
        if response.status_code != 200:
            raise ProviderError(f"EODHD returned {response.status_code}")

        payload: Any = response.json()
        if not isinstance(payload, list):
            raise ProviderError(f"EODHD returned {type(payload).__name__}, expected a list")
        return payload

    def fetch_daily(self, symbol: str) -> ProviderResult:
        if not self.is_configured():
            raise ProviderNotConfiguredError("EGX_EODHD_API_KEY is not set")

        rows = self._get(f"eod/{self._eodhd_symbol(symbol)}", {"period": "d", "order": "a"})

        candles: list[Candle] = []
        dropped: list[dt.date] = []
        for row in rows:
            try:
                day = dt.date.fromisoformat(str(row["date"]))
            except (KeyError, ValueError):
                continue
            try:
                candles.append(
                    Candle(
                        date=day,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row.get("volume") or 0),
                        adj_close=(
                            float(row["adjusted_close"])
                            if row.get("adjusted_close") is not None
                            else None
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # Same policy as the Yahoo parser: a bar that fails its own
                # invariants is feed corruption, not a bar. Drop, do not patch.
                dropped.append(day)

        candles.sort(key=lambda c: c.date)
        return ProviderResult(
            series=OHLCVSeries(symbol=symbol, candles=tuple(candles)),
            provider=self.name,
            raw_bar_count=len(rows),
            dropped_dates=tuple(dropped),
        )
