"""Append-only storage for recorded bars.

Intraday EGX data is irreplaceable — no vendor sells it and Yahoo has none — so
writes are additive and every bar records which source produced it. Re-recording
the same minute overwrites, because a later pass saw at least as many ticks.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

from egx_trader.data.intraday.models import BarInterval, IntradayBar

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol      VARCHAR NOT NULL,
    interval    VARCHAR NOT NULL,
    start       TIMESTAMPTZ NOT NULL,
    open        DOUBLE  NOT NULL,
    high        DOUBLE  NOT NULL,
    low         DOUBLE  NOT NULL,
    close       DOUBLE  NOT NULL,
    volume      BIGINT  NOT NULL,
    tick_count  INTEGER NOT NULL,
    source      VARCHAR NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, interval, start)
);
"""


class IntradayStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(path))
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> IntradayStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write(self, bars: list[IntradayBar]) -> int:
        """Upsert bars. Returns how many rows were written."""
        if not bars:
            return 0
        now = dt.datetime.now(dt.UTC)
        rows = [
            (
                b.symbol,
                b.interval.value,
                b.start,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.tick_count,
                b.source,
                now,
            )
            for b in bars
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO intraday_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        return len(rows)

    def read(
        self,
        symbol: str,
        interval: BarInterval = BarInterval.M1,
        *,
        since: dt.datetime | None = None,
    ) -> list[IntradayBar]:
        sql = (
            "SELECT symbol, interval, start, open, high, low, close, volume, "
            "tick_count, source FROM intraday_bars WHERE symbol = ? AND interval = ?"
        )
        params: list[object] = [symbol, interval.value]
        if since is not None:
            sql += " AND start >= ?"
            params.append(since)
        sql += " ORDER BY start"

        return [
            IntradayBar(
                symbol=r[0],
                interval=BarInterval(r[1]),
                start=r[2],
                open=r[3],
                high=r[4],
                low=r[5],
                close=r[6],
                volume=r[7],
                tick_count=r[8],
                source=r[9],
            )
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def coverage(self) -> list[tuple[str, str, int, dt.datetime, dt.datetime]]:
        """Per symbol and interval: bar count and the range recorded so far."""
        return self._conn.execute(
            "SELECT symbol, interval, COUNT(*), MIN(start), MAX(start) "
            "FROM intraday_bars GROUP BY symbol, interval ORDER BY symbol, interval"
        ).fetchall()

    def session_days(self) -> int:
        """Distinct Cairo dates recorded. This is the number that has to grow before
        any intraday strategy can be backtested."""
        result = self._conn.execute(
            "SELECT COUNT(DISTINCT CAST(start AT TIME ZONE 'Africa/Cairo' AS DATE)) "
            "FROM intraday_bars"
        ).fetchone()
        return int(result[0]) if result else 0
