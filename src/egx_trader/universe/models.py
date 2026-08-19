"""Instrument model for the EGX universe.

Two decisions here matter more than they look:

1. `sharia` is three-state, not a boolean. A prior implementation hardcoded
   `shariaCompliant: true` on every row, which is only "true" because the list was
   curated to contain nothing else — it carries no information. A boolean also
   forces a guess for names we have no labelling for, and a wrong "compliant" is
   the one error this system must never make.

2. Unknown never reads as permissive. An instrument with no `board` is not T+0
   eligible; an instrument with no `price_limit_pct` gets the tightest assumption.
   The safe direction is to refuse, not to allow.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUFFIX: Final = ".CA"


class ShariaStatus(StrEnum):
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    UNKNOWN = "unknown"
    """No sourced labelling. Treated as non-compliant everywhere it matters."""


class Board(StrEnum):
    MOST_ACTIVE = "most_active"
    MODERATE = "moderate"
    TAMAYUZ = "tamayuz"
    OTHER = "other"


class InstrumentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELISTED = "delisted"


# Per Thndr: names on the Most Active, Moderate Activity and Tamayuz SME boards
# can be T+0 traded. Everything else — including an unknown board — cannot.
T0_ELIGIBLE_BOARDS: Final[frozenset[Board]] = frozenset(
    {Board.MOST_ACTIVE, Board.MODERATE, Board.TAMAYUZ}
)


def normalize_symbol(raw: str) -> str:
    """`adib`, `ADIB`, `adib.ca` -> `ADIB.CA`."""
    symbol = raw.strip().upper()
    return symbol if symbol.endswith(SUFFIX) else f"{symbol}{SUFFIX}"


def symbol_code(raw: str) -> str:
    """`ADIB.CA` -> `ADIB`."""
    return raw.strip().upper().removesuffix(SUFFIX)


class Instrument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    name_en: str
    name_ar: str | None = None
    isin: str | None = None
    sector: str | None = None

    board: Board | None = None
    """Absent means unknown, which disables T+0 rather than assuming it."""

    sharia: ShariaStatus = ShariaStatus.UNKNOWN
    sharia_source: str | None = None
    sharia_as_of: dt.date | None = None

    index_weight: Annotated[float, Field(ge=0, le=100)] | None = None
    lot_size: Annotated[int, Field(gt=0)] | None = None
    tick_size: Annotated[float, Field(gt=0)] | None = None
    price_limit_pct: Annotated[float, Field(gt=0, le=100)] | None = None

    status: InstrumentStatus = InstrumentStatus.ACTIVE
    note: str | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_symbol(value)

    @property
    def code(self) -> str:
        """Bare code without the Reuters suffix."""
        return symbol_code(self.symbol)

    @property
    def is_tradable(self) -> bool:
        return self.status is InstrumentStatus.ACTIVE

    @property
    def t0_eligible(self) -> bool:
        """Whether this name can be bought and sold in the same session.

        Requires a known, eligible board. Unknown board -> False, deliberately.
        Note this is necessary but not sufficient: T+0 also needs an active Thndr
        Trader subscription and an advanced limit order.
        """
        return self.board in T0_ELIGIBLE_BOARDS

    @property
    def is_sharia_compliant(self) -> bool:
        """Strictly compliant. `unknown` is False — an unsourced label is not a label."""
        return self.sharia is ShariaStatus.COMPLIANT

    def missing_fields(self) -> list[str]:
        """Fields still unpopulated, for the coverage report."""
        return [
            name
            for name in ("isin", "sector", "board", "lot_size", "tick_size", "price_limit_pct")
            if getattr(self, name) is None
        ]
