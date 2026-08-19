"""Loading real positions from outside the repository.

Holdings are personal financial data. They were briefly hardcoded in the
dashboard snapshot, which put quantities and cost basis — the whole book — into
version control. Even in a private repo that is the wrong place for it: a repo
gets cloned, forked, backed up and shared, and the data outlives every one of
those decisions.

So positions live in a gitignored file the operator owns, and the code ships only
an example with obviously-fake numbers. Absent file means no positions, and the
dashboard says so rather than inventing any.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Position(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    qty: Annotated[int, Field(gt=0)]
    avg_cost: Annotated[float, Field(gt=0)]
    opened: dt.date | None = None
    note: str | None = None

    @property
    def book_cost(self) -> float:
        return round(self.qty * self.avg_cost, 2)


def load_positions(path: Path) -> list[Position]:
    """Read positions from `path`. Missing file is not an error — it means none."""
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    return [Position.model_validate(row) for row in raw.get("positions") or []]
