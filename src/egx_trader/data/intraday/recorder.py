"""The recording loop.

This exists because EGX intraday data cannot be bought or back-filled. Every
session that passes unrecorded is gone permanently, which inverts the usual
priority: staying alive and writing partial data beats failing cleanly.

So the loop never raises on a source hiccup — it records the failure, backs off,
and keeps going. The one exception is an expired session, which no amount of
retrying fixes and which a human has to clear.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from egx_trader.data.intraday.aggregator import aggregate
from egx_trader.data.intraday.models import BarInterval, Tick
from egx_trader.data.intraday.sources.base import (
    SessionExpiredError,
    TickSource,
    TickSourceError,
)
from egx_trader.data.intraday.store import IntradayStore
from egx_trader.market_calendar import CAIRO, EGXCalendar


@dataclass
class RecorderStats:
    polls: int = 0
    ticks: int = 0
    bars_written: int = 0
    failures: int = 0
    last_error: str | None = None
    started_at: dt.datetime | None = None
    stopped_reason: str | None = None
    symbols_seen: set[str] = field(default_factory=set)

    def summary(self) -> str:
        return (
            f"{self.polls} polls, {self.ticks} ticks, {self.bars_written} bars, "
            f"{self.failures} failures across {len(self.symbols_seen)} symbols"
        )


class IntradayRecorder:
    """Polls a tick source through the session and writes bars as buckets complete."""

    def __init__(
        self,
        source: TickSource,
        store: IntradayStore,
        symbols: list[str],
        *,
        interval: BarInterval = BarInterval.M1,
        poll_seconds: int = 20,
        calendar: EGXCalendar | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_consecutive_failures: int = 20,
    ) -> None:
        self._source = source
        self._store = store
        self._symbols = symbols
        self._interval = interval
        self._poll = poll_seconds
        self._cal = calendar or EGXCalendar(strict=False)
        self._clock = clock or (lambda: dt.datetime.now(CAIRO))
        self._sleep = sleeper
        self._max_failures = max_consecutive_failures
        self.stats = RecorderStats()

    def run_once(self, buffer: list[Tick]) -> int:
        """Poll once and append to the buffer. Returns how many ticks arrived."""
        ticks = self._source.poll(self._symbols)
        self.stats.polls += 1
        self.stats.ticks += len(ticks)
        for tick in ticks:
            self.stats.symbols_seen.add(tick.symbol)
        buffer.extend(ticks)
        return len(ticks)

    def flush(self, buffer: list[Tick], *, keep_open_bucket: bool = True) -> int:
        """Write completed buckets, keeping the in-progress one buffered.

        The newest bucket is still filling, so writing it now would persist a bar
        built from a fraction of its ticks. It stays in the buffer until the clock
        moves past it — except at session end, where there is no later flush.
        """
        if not buffer:
            return 0
        bars = aggregate(buffer, self._interval, calendar=self._cal)
        if not bars:
            buffer.clear()
            return 0

        if keep_open_bucket and len(bars) > 1:
            complete, open_start = bars[:-1], bars[-1].start
            buffer[:] = [t for t in buffer if t.at.astimezone(CAIRO) >= open_start]
        elif keep_open_bucket:
            return 0  # only the open bucket so far — nothing settled yet
        else:
            complete = bars
            buffer.clear()

        written = self._store.write(complete)
        self.stats.bars_written += written
        return written

    def run(self, *, until: dt.datetime | None = None) -> RecorderStats:
        """Record until the session closes, or `until`, or the source gives up."""
        self.stats.started_at = self._clock()
        buffer: list[Tick] = []
        consecutive = 0

        while True:
            now = self._clock()
            if until is not None and now >= until:
                self.stats.stopped_reason = "reached the requested end time"
                break
            if not self._cal.is_open(now):
                self.stats.stopped_reason = "session closed"
                break

            try:
                self.run_once(buffer)
                consecutive = 0
            except SessionExpiredError as exc:
                # No amount of retrying re-authenticates a browser session.
                self.stats.failures += 1
                self.stats.last_error = str(exc)
                self.stats.stopped_reason = "session expired — needs a human to log in"
                break
            except TickSourceError as exc:
                self.stats.failures += 1
                self.stats.last_error = str(exc)
                consecutive += 1
                if consecutive >= self._max_failures:
                    self.stats.stopped_reason = f"{consecutive} consecutive failures — giving up"
                    break

            self.flush(buffer)
            self._sleep(self._poll)

        # Session over: write the final partial bucket, since nothing follows it.
        self.flush(buffer, keep_open_bucket=False)
        return self.stats
