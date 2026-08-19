"""Intraday types.

Yahoo serves no intraday for EGX at all — it downgrades any interval to `1d` and
advertises no intraday range — so these bars can only ever be *recorded*, never
fetched. That single fact shapes everything here: the data is irreplaceable once
a session passes, so the recorder favours writing something honest over writing
something complete.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BarInterval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"

    @property
    def seconds(self) -> int:
        return {"1m": 60, "5m": 300, "15m": 900}[self.value]


class Tick(BaseModel):
    """One observation of a symbol's price at a moment in the session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    at: dt.datetime
    price: Annotated[float, Field(gt=0)]

    cumulative_volume: Annotated[int, Field(ge=0)] | None = None
    """Session-to-date share count as the venue reports it.

    Per-tick volume is derived by differencing consecutive observations rather
    than trusted directly, because a poll can miss trades between samples. If the
    venue only ever exposes a running total, differencing is the sole honest way
    to attribute volume to a bar.
    """

    bid: Annotated[float, Field(gt=0)] | None = None
    ask: Annotated[float, Field(gt=0)] | None = None
    source: str = "unknown"

    @model_validator(mode="after")
    def _require_tz(self) -> Tick:
        if self.at.tzinfo is None:
            raise ValueError(
                "Tick.at must be timezone-aware. A naive timestamp silently adopts "
                "the host clock, which is UTC on a server and Cairo on the Mac."
            )
        return self

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class IntradayBar(BaseModel):
    """An OHLCV bar built from ticks, stamped with where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    interval: BarInterval
    start: dt.datetime
    open: Annotated[float, Field(gt=0)]
    high: Annotated[float, Field(gt=0)]
    low: Annotated[float, Field(gt=0)]
    close: Annotated[float, Field(gt=0)]
    volume: Annotated[int, Field(ge=0)] = 0
    tick_count: Annotated[int, Field(ge=1)]
    source: str = "unknown"

    @model_validator(mode="after")
    def _check(self) -> IntradayBar:
        if self.start.tzinfo is None:
            raise ValueError("IntradayBar.start must be timezone-aware")
        if self.high < self.low:
            raise ValueError(f"{self.symbol} {self.start}: high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"{self.symbol} {self.start}: open outside the bar range")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"{self.symbol} {self.start}: close outside the bar range")
        return self

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(seconds=self.interval.seconds)

    @property
    def is_single_tick(self) -> bool:
        """A bar built from one observation. Real, but not a range — treat with care.

        Polling at 15-30s cannot see every print, so a thin name may contribute a
        single tick to a minute. The bar is not wrong, it is just low-resolution,
        and anything computing a true range off it should know that.
        """
        return self.tick_count == 1
