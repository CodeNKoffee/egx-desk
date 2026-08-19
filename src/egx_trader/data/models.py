"""Market data types.

A prior implementation kept only `{timestamps, closes, volumes}`
and discarded open/high/low — these carry full OHLCV. Without highs and lows there
is no ATR, no true-range stop, no gap detection and no Donchian channel, which
rules out the entire breakout strategy this project exists to run.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: dt.date
    open: Annotated[float, Field(gt=0)]
    high: Annotated[float, Field(gt=0)]
    low: Annotated[float, Field(gt=0)]
    close: Annotated[float, Field(gt=0)]
    volume: Annotated[int, Field(ge=0)]
    adj_close: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _check_ordering(self) -> Candle:
        if self.high < self.low:
            raise ValueError(f"{self.date}: high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"{self.date}: open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"{self.date}: close {self.close} outside [{self.low}, {self.high}]")
        return self

    @property
    def true_range_basis(self) -> float:
        """Intraday range. The other two true-range terms need the prior close."""
        return self.high - self.low

    @property
    def turnover(self) -> float:
        """Approximate traded value in EGP. The liquidity screen runs on this."""
        return self.close * self.volume


class Split(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: dt.date
    numerator: float
    denominator: float

    @property
    def ratio(self) -> float:
        return self.numerator / self.denominator


class Dividend(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: dt.date
    amount: float


class OHLCVSeries(BaseModel):
    """A symbol's daily bars, ascending by date."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    currency: str = "EGP"
    candles: tuple[Candle, ...]
    splits: tuple[Split, ...] = ()
    dividends: tuple[Dividend, ...] = ()

    instrument_type: str | None = None
    """Yahoo's `quoteType`. EGX names come back as MUTUALFUND, which is wrong and
    a hint that this feed treats them as second-class."""

    exchange_timestamp: dt.datetime | None = None
    """Yahoo's `regularMarketTime`. Often badly stale for `.CA` — checked in quality."""

    @model_validator(mode="after")
    def _check_ascending(self) -> OHLCVSeries:
        dates = [c.date for c in self.candles]
        if dates != sorted(dates):
            raise ValueError(f"{self.symbol}: candles must be ascending by date")
        if len(set(dates)) != len(dates):
            raise ValueError(f"{self.symbol}: duplicate candle dates")
        return self

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def first_date(self) -> dt.date | None:
        return self.candles[0].date if self.candles else None

    @property
    def last_date(self) -> dt.date | None:
        return self.candles[-1].date if self.candles else None

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def volumes(self) -> list[int]:
        return [c.volume for c in self.candles]

    def split_on(self, when: dt.date) -> Split | None:
        return next((s for s in self.splits if s.date == when), None)
