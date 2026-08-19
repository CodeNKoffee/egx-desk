"""Loading and querying the EGX instrument master."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from egx_trader.config import UniverseMode
from egx_trader.universe.models import (
    Instrument,
    InstrumentStatus,
    ShariaStatus,
    normalize_symbol,
)

_DEFAULT_INSTRUMENTS_FILE: Final = Path(__file__).parent / "instruments.yaml"


class UnknownInstrumentError(KeyError):
    """Symbol is not in the instrument master.

    Raised rather than defaulted: trading a symbol we hold no metadata for means
    trading without settlement, liquidity or compliance information.
    """


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """What the master does and does not know. Surfaced so gaps stay visible."""

    total: int
    tradable: int
    by_sharia: dict[ShariaStatus, int]
    by_status: dict[InstrumentStatus, int]
    missing_by_field: dict[str, int]
    t0_eligible: int

    def summary_lines(self) -> list[str]:
        lines = [
            f"{self.total} instruments ({self.tradable} tradable)",
            "  sharia:  " + ", ".join(f"{k.value}={v}" for k, v in sorted(self.by_sharia.items())),
            "  status:  " + ", ".join(f"{k.value}={v}" for k, v in sorted(self.by_status.items())),
            f"  T+0 eligible: {self.t0_eligible}",
        ]
        if self.missing_by_field:
            lines.append("  missing fields:")
            lines += [
                f"    {field}: {count} instruments"
                for field, count in sorted(self.missing_by_field.items(), key=lambda kv: -kv[1])
            ]
        return lines


class InstrumentRegistry:
    """The instrument master, loaded from YAML.

    `universe(mode)` is the Sharia switch: `SHARIA` yields only instruments
    positively labelled compliant — `unknown` is excluded alongside
    `non_compliant`, because an unsourced label is not a label.
    """

    def __init__(self, instruments: list[Instrument]) -> None:
        self._by_symbol: dict[str, Instrument] = {}
        for instrument in instruments:
            if instrument.symbol in self._by_symbol:
                raise ValueError(f"duplicate symbol in instrument master: {instrument.symbol}")
            self._by_symbol[instrument.symbol] = instrument

    @classmethod
    def load(cls, path: Path | None = None) -> InstrumentRegistry:
        raw = yaml.safe_load((path or _DEFAULT_INSTRUMENTS_FILE).read_text())
        return cls([Instrument.model_validate(row) for row in raw["instruments"]])

    # ── Lookup ───────────────────────────────────────────────────────────────

    def get(self, symbol: str) -> Instrument | None:
        return self._by_symbol.get(normalize_symbol(symbol))

    def require(self, symbol: str) -> Instrument:
        instrument = self.get(symbol)
        if instrument is None:
            raise UnknownInstrumentError(
                f"{normalize_symbol(symbol)} is not in the instrument master. "
                "Add it to instruments.yaml — trading a symbol with no metadata means "
                "trading with no settlement, liquidity or compliance information."
            )
        return instrument

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and normalize_symbol(symbol) in self._by_symbol

    def __len__(self) -> int:
        return len(self._by_symbol)

    def __iter__(self) -> Iterator[Instrument]:
        return iter(self._by_symbol.values())

    # ── Selection ────────────────────────────────────────────────────────────

    def all(self) -> list[Instrument]:
        return list(self._by_symbol.values())

    def tradable(self) -> list[Instrument]:
        return [i for i in self._by_symbol.values() if i.is_tradable]

    def universe(self, mode: UniverseMode) -> list[Instrument]:
        """The tradable set for a given mode. This is the Sharia switch."""
        candidates = self.tradable()
        if mode is UniverseMode.SHARIA:
            return [i for i in candidates if i.is_sharia_compliant]
        return candidates

    def symbols(self, mode: UniverseMode) -> list[str]:
        return [i.symbol for i in self.universe(mode)]

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def coverage(self) -> CoverageReport:
        by_sharia: dict[ShariaStatus, int] = {}
        by_status: dict[InstrumentStatus, int] = {}
        missing_by_field: dict[str, int] = {}

        for instrument in self._by_symbol.values():
            by_sharia[instrument.sharia] = by_sharia.get(instrument.sharia, 0) + 1
            by_status[instrument.status] = by_status.get(instrument.status, 0) + 1
            for field in instrument.missing_fields():
                missing_by_field[field] = missing_by_field.get(field, 0) + 1

        return CoverageReport(
            total=len(self._by_symbol),
            tradable=len(self.tradable()),
            by_sharia=by_sharia,
            by_status=by_status,
            missing_by_field=missing_by_field,
            t0_eligible=sum(1 for i in self._by_symbol.values() if i.t0_eligible),
        )
