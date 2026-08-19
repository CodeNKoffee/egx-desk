"""Roll ticks into intraday bars.

Two rules matter more than the arithmetic:

**Bars never span a session boundary.** A bucket is closed by the clock, but it is
also closed by the market closing. Letting 14:29 and the next morning's 10:00 land
in one bar would manufacture an overnight range that never traded.

**A period with no ticks produces no bar.** Not a zero-volume bar, not a
forward-filled one — nothing. The same rule the daily layer follows: absence of
data is not a price, and inventing one puts a fabricated print into every
indicator downstream.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from egx_trader.data.intraday.models import BarInterval, IntradayBar, Tick
from egx_trader.market_calendar import CAIRO, EGXCalendar


def bucket_start(when: dt.datetime, interval: BarInterval) -> dt.datetime:
    """Floor a timestamp to the start of its bucket, in Cairo time.

    Bucketing in Cairo rather than UTC keeps bar boundaries aligned to the session
    clock. EGX's phases fall on Cairo minutes, and a 5-minute bucket floored in a
    zone offset by a non-whole number of hours would straddle them.
    """
    local = when.astimezone(CAIRO)
    seconds = interval.seconds
    floored = (local.hour * 3600 + local.minute * 60 + local.second) // seconds * seconds
    return local.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(seconds=floored)


def aggregate(
    ticks: Iterable[Tick],
    interval: BarInterval,
    *,
    calendar: EGXCalendar | None = None,
    session_only: bool = True,
) -> list[IntradayBar]:
    """Build bars from ticks, ascending by time.

    With `session_only`, ticks outside a trading phase are discarded rather than
    bucketed — an out-of-hours quote is a stale screen value, not a trade.
    """
    cal = calendar or EGXCalendar(strict=False)
    ordered = sorted(ticks, key=lambda t: t.at)

    bars: list[IntradayBar] = []
    bucket: list[Tick] = []
    current: dt.datetime | None = None

    def flush() -> None:
        if not bucket or current is None:
            return
        prices = [t.price for t in bucket]
        bars.append(
            IntradayBar(
                symbol=bucket[0].symbol,
                interval=interval,
                start=current,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=_volume_for(bucket),
                tick_count=len(bucket),
                source=bucket[0].source,
            )
        )

    for tick in ordered:
        if session_only and not cal.is_open(tick.at):
            continue
        start = bucket_start(tick.at, interval)
        if current is not None and start != current:
            flush()
            bucket = []
        current = start
        bucket.append(tick)

    flush()
    return bars


def _volume_for(bucket: list[Tick]) -> int:
    """Volume traded inside a bucket, differenced from the running session total.

    Venues typically publish cumulative volume, so the increment across a bucket is
    what actually traded in it. A decrease means the counter reset — a new session,
    or a reconnect that re-read from zero — and the honest answer there is 0 rather
    than a negative or a wildly inflated figure.
    """
    totals = [t.cumulative_volume for t in bucket if t.cumulative_volume is not None]
    if len(totals) < 2:
        return 0
    delta = totals[-1] - totals[0]
    return max(0, delta)


def merge_bars(existing: list[IntradayBar], incoming: list[IntradayBar]) -> list[IntradayBar]:
    """Combine two runs of bars, newer winning on collision.

    The recorder restarts — a dropped session, a re-login, a crash — and re-records
    minutes it already has. A later pass saw at least as many ticks as an earlier
    one, so it is the better record of that minute.
    """
    by_key: dict[tuple[str, dt.datetime], IntradayBar] = {(b.symbol, b.start): b for b in existing}
    for bar in incoming:
        by_key[(bar.symbol, bar.start)] = bar
    return [by_key[k] for k in sorted(by_key, key=lambda k: (k[1], k[0]))]
