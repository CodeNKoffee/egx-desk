"""Provider layer tests.

The merge behaviour matters most: order is priority, and every bar must carry
provenance. Without knowing which vendor supplied a bar, a backtest cannot tell a
real edge from one vendor's artefact.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from egx_trader.data.models import Candle, OHLCVSeries
from egx_trader.data.providers import (
    EODHDProvider,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderResult,
    TradingViewCSVProvider,
    merge_providers,
)


def candle(day: dt.date, close: float) -> Candle:
    return Candle(
        date=day, open=close, high=close * 1.01, low=close * 0.99, close=close, volume=100
    )


class FakeProvider:
    """Stand-in provider driven by an explicit date->close map."""

    def __init__(
        self,
        name: str,
        closes: dict[dt.date, float] | None = None,
        *,
        configured: bool = True,
        error: str | None = None,
    ) -> None:
        self.name = name
        self._closes = closes or {}
        self._configured = configured
        self._error = error

    def is_configured(self) -> bool:
        return self._configured

    def fetch_daily(self, symbol: str) -> ProviderResult:
        if self._error:
            raise ProviderError(self._error)
        return ProviderResult(
            series=OHLCVSeries(
                symbol=symbol,
                candles=tuple(candle(d, c) for d, c in sorted(self._closes.items())),
            ),
            provider=self.name,
        )


D1, D2, D3 = dt.date(2026, 8, 2), dt.date(2026, 8, 3), dt.date(2026, 8, 4)


class TestMerge:
    def test_first_provider_wins_on_overlap(self) -> None:
        primary = FakeProvider("yahoo", {D1: 10.0, D2: 11.0})
        backup = FakeProvider("eodhd", {D1: 99.0, D2: 99.0})
        merged = merge_providers("X.CA", [primary, backup])

        assert [c.close for c in merged.series.candles] == [10.0, 11.0]
        assert set(merged.sources.values()) == {"yahoo"}

    def test_later_provider_fills_only_the_gaps(self) -> None:
        """This is what `yahoo,eodhd` is for: patch the 22-30% Yahoo loses."""
        primary = FakeProvider("yahoo", {D1: 10.0, D3: 12.0})
        backup = FakeProvider("eodhd", {D1: 99.0, D2: 11.0, D3: 99.0})
        merged = merge_providers("X.CA", [primary, backup])

        assert len(merged.series) == 3
        assert merged.sources == {D1: "yahoo", D2: "eodhd", D3: "yahoo"}
        assert merged.contributions == {"yahoo": 2, "eodhd": 1}

    def test_every_bar_carries_provenance(self) -> None:
        merged = merge_providers(
            "X.CA", [FakeProvider("yahoo", {D1: 1.0}), FakeProvider("eodhd", {D2: 2.0})]
        )
        for bar in merged.series.candles:
            assert bar.date in merged.sources

    def test_unconfigured_providers_are_recorded_not_silently_skipped(self) -> None:
        merged = merge_providers(
            "X.CA",
            [FakeProvider("yahoo", {D1: 1.0}), FakeProvider("eodhd", configured=False)],
        )
        assert merged.failures["eodhd"] == "not configured"

    def test_a_failing_provider_does_not_lose_the_others(self) -> None:
        merged = merge_providers(
            "X.CA",
            [FakeProvider("eodhd", error="boom"), FakeProvider("yahoo", {D1: 1.0})],
        )
        assert len(merged.series) == 1
        assert "boom" in merged.failures["eodhd"]

    def test_all_providers_failing_raises_with_the_reasons(self) -> None:
        with pytest.raises(ProviderError, match="no provider returned bars"):
            merge_providers("X.CA", [FakeProvider("eodhd", error="401 unauthorized")])

    def test_merged_series_stays_ordered(self) -> None:
        merged = merge_providers(
            "X.CA",
            [FakeProvider("a", {D3: 3.0}), FakeProvider("b", {D1: 1.0, D2: 2.0})],
        )
        dates = [c.date for c in merged.series.candles]
        assert dates == sorted(dates)

    def test_provenance_summary_reads(self) -> None:
        merged = merge_providers(
            "X.CA", [FakeProvider("yahoo", {D1: 1.0}), FakeProvider("eodhd", {D2: 2.0})]
        )
        assert "yahoo=1" in merged.provenance_summary()


class TestEODHD:
    def test_unconfigured_refuses(self) -> None:
        provider = EODHDProvider("")
        assert provider.is_configured() is False
        with pytest.raises(ProviderNotConfiguredError, match="EODHD_API_KEY"):
            provider.fetch_daily("BIOC.CA")

    def test_symbol_is_translated_to_the_egx_suffix(self) -> None:
        """EODHD uses `.EGX`, Yahoo uses `.CA`. Same instrument, different spelling."""
        payload = [
            {"date": "2026-08-02", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10}
        ]
        with respx.mock(base_url="https://eodhd.com") as mock:
            route = mock.get("/api/eod/BIOC.EGX").respond(json=payload)
            with EODHDProvider("k") as provider:
                provider.fetch_daily("BIOC.CA")
            assert route.called

    def test_parses_bars(self) -> None:
        payload = [
            {
                "date": "2026-08-02",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 1000,
                "adjusted_close": 10.4,
            }
        ]
        with respx.mock(base_url="https://eodhd.com") as mock:
            mock.get("/api/eod/BIOC.EGX").respond(json=payload)
            with EODHDProvider("k") as provider:
                result = provider.fetch_daily("BIOC.CA")
        bar = result.series.candles[0]
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (10, 11, 9, 10.5, 1000)
        assert bar.adj_close == 10.4

    def test_corrupt_rows_are_dropped_not_patched(self) -> None:
        payload = [
            {"date": "2026-08-02", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
            {"date": "2026-08-03", "open": 10, "high": 5, "low": 9, "close": 10.5, "volume": 1},
        ]
        with respx.mock(base_url="https://eodhd.com") as mock:
            mock.get("/api/eod/BIOC.EGX").respond(json=payload)
            with EODHDProvider("k") as provider:
                result = provider.fetch_daily("BIOC.CA")
        assert len(result.series) == 1
        assert result.dropped_dates == (dt.date(2026, 8, 3),)

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failure_says_egx_needs_a_paid_plan(self, status: int) -> None:
        with respx.mock(base_url="https://eodhd.com") as mock:
            mock.get("/api/eod/BIOC.EGX").respond(status_code=status)
            with (
                EODHDProvider("k") as provider,
                pytest.raises(ProviderNotConfiguredError, match="paid plan"),
            ):
                provider.fetch_daily("BIOC.CA")

    def test_transient_failure_is_retried(self) -> None:
        payload = [
            {"date": "2026-08-02", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1}
        ]
        with respx.mock(base_url="https://eodhd.com") as mock:
            mock.get("/api/eod/BIOC.EGX").mock(
                side_effect=[httpx.Response(503), httpx.Response(200, json=payload)]
            )
            with EODHDProvider("k") as provider:
                assert len(provider.fetch_daily("BIOC.CA").series) == 1


class TestTradingViewCSV:
    def _write(self, tmp_path: Path, name: str, body: str) -> TradingViewCSVProvider:
        (tmp_path / name).write_text(body)
        return TradingViewCSVProvider(tmp_path)

    def test_reads_a_standard_export(self, tmp_path: Path) -> None:
        provider = self._write(
            tmp_path,
            "BIOC.csv",
            "time,open,high,low,close,Volume\n2026-08-02,10,11,9,10.5,1000\n",
        )
        result = provider.fetch_daily("BIOC.CA")
        assert result.series.candles[0].close == 10.5

    def test_matches_tradingviews_own_filename_shape(self, tmp_path: Path) -> None:
        """TradingView exports as e.g. `EGX_BIOC, 1D.csv`."""
        provider = self._write(
            tmp_path,
            "EGX_BIOC, 1D.csv",
            "time,open,high,low,close,Volume\n2026-08-02,10,11,9,10.5,1000\n",
        )
        assert len(provider.fetch_daily("BIOC").series) == 1

    def test_accepts_unix_timestamps(self, tmp_path: Path) -> None:
        provider = self._write(
            tmp_path, "BIOC.csv", "time,open,high,low,close\n1785974400,10,11,9,10.5\n"
        )
        assert len(provider.fetch_daily("BIOC.CA").series) == 1

    def test_missing_volume_column_is_tolerated(self, tmp_path: Path) -> None:
        provider = self._write(
            tmp_path, "BIOC.csv", "time,open,high,low,close\n2026-08-02,10,11,9,10.5\n"
        )
        assert provider.fetch_daily("BIOC.CA").series.candles[0].volume == 0

    def test_duplicate_dates_collapse(self, tmp_path: Path) -> None:
        """Intraday exports carry many rows per day; the series must stay daily."""
        provider = self._write(
            tmp_path,
            "BIOC.csv",
            "time,open,high,low,close\n"
            "2026-08-02T10:00:00,10,11,9,10.0\n"
            "2026-08-02T14:00:00,10,11,9,10.5\n",
        )
        result = provider.fetch_daily("BIOC.CA")
        assert len(result.series) == 1
        assert result.series.candles[0].close == 10.5

    def test_missing_directory_explains_how_to_export(self, tmp_path: Path) -> None:
        provider = TradingViewCSVProvider(tmp_path / "nope")
        with pytest.raises(ProviderNotConfiguredError, match="Export chart data"):
            provider.fetch_daily("BIOC.CA")

    def test_missing_symbol_file_is_a_plain_error(self, tmp_path: Path) -> None:
        provider = self._write(tmp_path, "ADIB.csv", "time,open,high,low,close\n")
        with pytest.raises(ProviderError, match="no TradingView export"):
            provider.fetch_daily("BIOC.CA")

    def test_is_configured_requires_actual_csvs(self, tmp_path: Path) -> None:
        assert TradingViewCSVProvider(tmp_path).is_configured() is False
        (tmp_path / "BIOC.csv").write_text("time,open,high,low,close\n")
        assert TradingViewCSVProvider(tmp_path).is_configured() is True
