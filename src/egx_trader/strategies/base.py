"""Strategy protocol.

A strategy sees history up to and including bar `i` and returns an intent for the
*next* bar. It never sees bar i+1 — the backtest enforces that by construction
rather than trusting each strategy to behave, because look-ahead is the single
easiest way to produce a backtest that looks brilliant and loses money.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from egx_trader.data.models import OHLCVSeries


class Signal(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class Intent:
    """What a strategy wants to do at the next open."""

    signal: Signal
    when: dt.date
    reason: str = ""
    stop_price: float | None = None
    features: dict[str, float] = field(default_factory=dict)
    """Snapshot of what drove the decision. Without this a backtest result is a
    number with no way to ask why."""


@runtime_checkable
class Strategy(Protocol):
    name: str

    def prepare(self, series: OHLCVSeries) -> None:
        """Precompute indicators once per symbol."""
        ...

    def evaluate(
        self, series: OHLCVSeries, i: int, *, in_position: bool, entry_index: int | None
    ) -> Intent:
        """Decide using bars 0..i only."""
        ...
