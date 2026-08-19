"""EGX instrument master and the Sharia / all universe switch."""

from egx_trader.universe.models import (
    T0_ELIGIBLE_BOARDS,
    Board,
    Instrument,
    InstrumentStatus,
    ShariaStatus,
    normalize_symbol,
    symbol_code,
)
from egx_trader.universe.registry import (
    CoverageReport,
    InstrumentRegistry,
    UnknownInstrumentError,
)

__all__ = [
    "T0_ELIGIBLE_BOARDS",
    "Board",
    "CoverageReport",
    "Instrument",
    "InstrumentRegistry",
    "InstrumentStatus",
    "ShariaStatus",
    "UnknownInstrumentError",
    "normalize_symbol",
    "symbol_code",
]
