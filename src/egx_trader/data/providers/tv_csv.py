"""TradingView CSV provider — a manual import, deliberately not a client.

TradingView has genuine EGX coverage but **no data API at any subscription
tier**, and its terms forbid automated collection via "scripts, APIs, screen
scraping, data mining, robots... regardless of their intended purposes", with
market data licensed display-only. Community MCP servers that wrap its internal
websocket exist and get accounts banned.

What it does offer, on Plus and Premium, is an official *Export chart data*
button. So this provider reads CSVs a human exported. No network calls, nothing
to rate-limit, nothing that risks the account.

Drop exports into the configured directory named by symbol — `BIOC.csv`,
`BIOC.CA.csv` and `EGX_BIOC, 1D.csv` (TradingView's own default) all resolve.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Final

from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.data.providers.base import (
    ProviderError,
    ProviderNotConfiguredError,
    ProviderResult,
)
from egx_trader.universe.models import symbol_code

# TradingView's export headers vary by locale and chart setup.
_DATE_KEYS: Final = ("time", "date", "datetime")
_FIELD_ALIASES: Final = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "price"),
    "volume": ("volume", "vol", "v"),
}


def _parse_date(raw: str) -> dt.date | None:
    text = raw.strip().strip('"')
    if not text:
        return None
    if text.isdigit():  # unix seconds
        return dt.datetime.fromtimestamp(int(text), tz=dt.UTC).date()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
    # ISO with an offset, e.g. 2026-08-13T00:00:00+03:00
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _pick(row: dict[str, str], names: tuple[str, ...]) -> str | None:
    for key, value in row.items():
        if key and key.strip().lower() in names:
            return value
    return None


class TradingViewCSVProvider:
    """Reads TradingView 'Export chart data' CSVs from a directory."""

    name = "tradingview_csv"

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def is_configured(self) -> bool:
        return self._dir.is_dir() and any(self._dir.glob("*.csv"))

    def _find_file(self, symbol: str) -> Path | None:
        code = symbol_code(symbol).upper()
        if not self._dir.is_dir():
            return None
        for path in sorted(self._dir.glob("*.csv")):
            stem = path.stem.upper()
            # Matches BIOC, BIOC.CA, EGX_BIOC, "EGX_BIOC, 1D"
            if stem == code or stem.startswith((f"{code}.", f"{code},", f"{code}_")):
                return path
            if f"_{code}" in stem or stem.startswith(f"EGX_{code}"):
                return path
        return None

    def fetch_daily(self, symbol: str) -> ProviderResult:
        if not self._dir.is_dir():
            raise ProviderNotConfiguredError(
                f"{self._dir} does not exist. Export chart data from TradingView "
                "(three dots -> Export chart data, needs Plus or Premium) and drop "
                "the CSVs there."
            )
        path = self._find_file(symbol)
        if path is None:
            raise ProviderError(f"no TradingView export found for {symbol} in {self._dir}")

        candles: list[Candle] = []
        dropped: list[dt.date] = []
        raw_rows = 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_rows += 1
                raw_date = next(
                    (row[k] for k in row if k and k.strip().lower() in _DATE_KEYS), None
                )
                day = _parse_date(raw_date) if raw_date else None
                if day is None:
                    continue
                values: dict[str, float] = {}
                try:
                    for field_name, aliases in _FIELD_ALIASES.items():
                        raw = _pick(row, aliases)
                        if raw is None or not raw.strip():
                            if field_name == "volume":
                                values[field_name] = 0.0
                                continue
                            raise ValueError(f"missing {field_name}")
                        values[field_name] = float(raw.replace(",", ""))
                    candles.append(
                        Candle(
                            date=day,
                            open=values["open"],
                            high=values["high"],
                            low=values["low"],
                            close=values["close"],
                            volume=int(values["volume"]),
                        )
                    )
                except (ValueError, KeyError):
                    dropped.append(day)

        # Exports sometimes carry several rows per day (intraday charts). Keep the
        # last row for each date rather than letting OHLCVSeries reject duplicates.
        deduped: dict[dt.date, Candle] = {c.date: c for c in candles}
        ordered = tuple(deduped[d] for d in sorted(deduped))

        return ProviderResult(
            series=OHLCVSeries(symbol=symbol, candles=ordered),
            provider=self.name,
            raw_bar_count=raw_rows,
            dropped_dates=tuple(dropped),
            notes=(f"from {path.name}",),
        )
