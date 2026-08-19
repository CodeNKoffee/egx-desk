"""Where ticks come from."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from egx_trader.data.intraday.models import Tick


class TickSourceError(RuntimeError):
    """The source could not produce a reading."""


class SessionExpiredError(TickSourceError):
    """Authentication lapsed. Needs a human to log back in; retrying will not help."""


@runtime_checkable
class TickSource(Protocol):
    """Anything that can report current prices for a set of symbols."""

    name: str

    def is_ready(self) -> bool:
        """False when unauthenticated or unreachable, so the recorder can back off."""
        ...

    def poll(self, symbols: list[str]) -> list[Tick]:
        """One reading per symbol. Partial results are fine — a missing symbol is
        simply absent, never a fabricated price."""
        ...

    def close(self) -> None: ...
