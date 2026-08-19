"""Validation gates for daily EGX bars.

The strongest gate here is market-specific: **EGX enforces daily price limits**
(±20% on the most-active board, tighter elsewhere, with a ±10% MVWAP circuit
breaker that halts a stock for 10 minutes). The band is visibly binding in the
data — BIOC printed exactly -20.0% then +20.0% on consecutive sessions during its
2026 run — which gives a hard, exchange-derived line between a real price move and
a broken one, rather than a generic "look for big jumps" heuristic.

A caveat learned the expensive way, and the reason `_check_price_jumps` takes a
calendar: **consecutive bars are not always consecutive sessions.** Yahoo's EGX
history skips trading days at a median rate of 27% across the universe. BIOC's
2026-08-04 bar looks like a +148.8% day and is really four sessions of a parabolic
run with the days between missing. Judging that against a one-day band flagged 296
"corrupt" bars across 59 of 71 symbols — nearly all of them false. The allowance
compounds over sessions actually elapsed.

(An earlier reading of a saved scan treated
BIOC at 345.25 against an MA20 of 134 as unadjusted split data. It was not: 345.25
was the genuine open on 2026-08-04, mid-run, and the MA20 was simply far below a
vertical price. Worth remembering before blaming the feed.)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from egx_trader.data.models import OHLCVSeries
from egx_trader.market_calendar import EGXCalendar

# EGX's widest published daily price limit is ±20% (most-active board), and the
# band is visibly binding in the data: BIOC printed exactly -20.0% then +20.0% on
# consecutive sessions during its 2026 run. No cushion is needed now that the gate
# compounds the allowance over however many sessions a bar step actually spans.
_EGX_MAX_DAILY_MOVE_PCT: Final = 20.0

# A split reported a few days off the price jump still explains it — exchange and
# vendor dates disagree regularly. Widened from 3 after live data showed ADIB.CA
# dropping 49.5% on 2025-05-26 against a split Yahoo dated 2025-06-04, nine days
# later. Egyptian bonus-share and capital-increase events are especially prone to
# this, since the ex-date and the vendor's recorded date rarely line up.
_SPLIT_MATCH_WINDOW_DAYS: Final = 10


class Severity(StrEnum):
    WARNING = "warning"
    """Usable, but something is off. Recorded, not blocking."""

    ERROR = "error"
    """Not safe to trade on. Blocks the symbol."""


class IssueCode(StrEnum):
    NO_DATA = "no_data"
    INSUFFICIENT_HISTORY = "insufficient_history"
    HIGH_DROP_RATE = "high_drop_rate"
    UNEXPLAINED_PRICE_JUMP = "unexplained_price_jump"
    STALE_EXCHANGE_TIME = "stale_exchange_time"
    UNEXPECTED_INSTRUMENT_TYPE = "unexpected_instrument_type"
    ZERO_VOLUME_RUN = "zero_volume_run"
    FLATLINED_PRICE = "flatlined_price"
    STALE_LAST_BAR = "stale_last_bar"


@dataclass(frozen=True, slots=True)
class Issue:
    code: IssueCode
    severity: Severity
    message: str
    when: dt.date | None = None

    def __str__(self) -> str:
        where = f" [{self.when}]" if self.when else ""
        return f"{self.severity.value.upper()}{where} {self.code.value}: {self.message}"


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    min_bars: int = 60
    """55 for the Donchian channel plus headroom."""

    warn_drop_fraction: float = 0.05
    error_drop_fraction: float = 0.25
    max_daily_move_pct: float = _EGX_MAX_DAILY_MOVE_PCT
    max_exchange_staleness_days: int = 5
    max_last_bar_staleness_sessions: int = 5
    max_zero_volume_fraction: float = 0.30
    flatline_run_length: int = 10
    expected_instrument_types: frozenset[str] = frozenset({"EQUITY"})


@dataclass(frozen=True, slots=True)
class QualityReport:
    symbol: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def is_usable(self) -> bool:
        """False when any error-level issue was found. Errors block trading."""
        return not self.errors

    def has(self, code: IssueCode) -> bool:
        return any(i.code is code for i in self.issues)

    def summary(self) -> str:
        if not self.issues:
            return f"{self.symbol}: clean"
        return f"{self.symbol}: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"


def _split_explains(series: OHLCVSeries, when: dt.date, observed_ratio: float) -> bool:
    """Whether a reported split near `when` accounts for the observed price ratio."""
    for split in series.splits:
        if abs((split.date - when).days) > _SPLIT_MATCH_WINDOW_DAYS:
            continue
        for candidate in (split.ratio, 1 / split.ratio if split.ratio else 0):
            if candidate and 0.7 <= observed_ratio / candidate <= 1.3:
                return True
    return False


def _check_history_length(series: OHLCVSeries, t: QualityThresholds) -> list[Issue]:
    if len(series) >= t.min_bars:
        return []
    return [
        Issue(
            IssueCode.INSUFFICIENT_HISTORY,
            Severity.ERROR,
            f"{len(series)} bars, need {t.min_bars} for the indicator set",
        )
    ]


def _check_drop_rate(dropped_fraction: float, t: QualityThresholds) -> list[Issue]:
    if dropped_fraction >= t.error_drop_fraction:
        severity = Severity.ERROR
    elif dropped_fraction >= t.warn_drop_fraction:
        severity = Severity.WARNING
    else:
        return []
    return [
        Issue(
            IssueCode.HIGH_DROP_RATE,
            severity,
            f"{dropped_fraction:.1%} of bars were unusable",
        )
    ]


def _check_price_jumps(
    series: OHLCVSeries,
    t: QualityThresholds,
    calendar: EGXCalendar | None = None,
) -> list[Issue]:
    """Flag close-to-close moves that EGX's price limits make impossible.

    Consecutive *bars* are not always consecutive *sessions*. Yahoo's EGX history
    skips trading days — a median 27% of them across the universe — so a single bar
    step can span several sessions. BIOC printed a "+148.8%" bar on 2026-08-04 that
    was really four sessions of a parabolic run with the days between missing.

    The allowance therefore compounds over the number of EGX sessions actually
    elapsed: a name can legitimately gain 20% a day for four days and be up 107%.
    Without a calendar the check falls back to treating every step as one session,
    which is the strict reading.
    """
    issues: list[Issue] = []
    limit = t.max_daily_move_pct / 100

    for previous, current in zip(series.candles, series.candles[1:], strict=False):
        if previous.close <= 0:
            continue

        sessions = 1
        if calendar is not None:
            sessions = max(1, calendar.sessions_between(previous.date, current.date))

        allowed = (1 + limit) ** sessions - 1
        change = (current.close - previous.close) / previous.close
        if abs(change) <= allowed:
            continue
        if _split_explains(series, current.date, current.close / previous.close):
            continue

        span = f"over {sessions} sessions" if sessions > 1 else "in one session"
        issues.append(
            Issue(
                IssueCode.UNEXPLAINED_PRICE_JUMP,
                Severity.ERROR,
                f"close moved {change * 100:+.1f}% ({previous.close:g} -> {current.close:g}) "
                f"{span}, beyond the ±{allowed * 100:.0f}% that EGX's daily price band "
                f"permits over that span, and no split explains it — a corporate action, "
                f"more missing sessions than the calendar knows about, or bad data",
                when=current.date,
            )
        )
    return issues


def _check_feed_metadata(
    series: OHLCVSeries, t: QualityThresholds, as_of: dt.date | None
) -> list[Issue]:
    issues: list[Issue] = []

    if series.instrument_type and series.instrument_type not in t.expected_instrument_types:
        issues.append(
            Issue(
                IssueCode.UNEXPECTED_INSTRUMENT_TYPE,
                Severity.WARNING,
                f"upstream types this as {series.instrument_type}, expected "
                f"{'/'.join(sorted(t.expected_instrument_types))} — EGX equities are "
                "commonly mislabelled MUTUALFUND by this feed",
            )
        )

    reference = as_of or series.last_date
    if series.exchange_timestamp and reference:
        staleness = (reference - series.exchange_timestamp.date()).days
        if staleness > t.max_exchange_staleness_days:
            issues.append(
                Issue(
                    IssueCode.STALE_EXCHANGE_TIME,
                    Severity.WARNING,
                    f"upstream regularMarketTime is {staleness} days behind the last bar",
                )
            )

    if as_of and series.last_date:
        gap_days = (as_of - series.last_date).days
        if gap_days > t.max_last_bar_staleness_sessions * 2:
            issues.append(
                Issue(
                    IssueCode.STALE_LAST_BAR,
                    Severity.ERROR,
                    f"last bar is {series.last_date}, {gap_days} calendar days before {as_of}",
                )
            )

    return issues


def _check_dead_feed(series: OHLCVSeries, t: QualityThresholds) -> list[Issue]:
    issues: list[Issue] = []

    zero_fraction = sum(1 for c in series.candles if c.volume == 0) / len(series)
    if zero_fraction > t.max_zero_volume_fraction:
        issues.append(
            Issue(
                IssueCode.ZERO_VOLUME_RUN,
                Severity.WARNING,
                f"{zero_fraction:.0%} of bars have zero volume — thin, halted, or a "
                "synthesised series",
            )
        )

    run = 1
    for previous, current in zip(series.candles, series.candles[1:], strict=False):
        run = run + 1 if current.close == previous.close else 1
        if run >= t.flatline_run_length:
            issues.append(
                Issue(
                    IssueCode.FLATLINED_PRICE,
                    Severity.WARNING,
                    f"close unchanged for {run} consecutive bars — likely a stuck feed",
                    when=current.date,
                )
            )
            break

    return issues


def check_series(
    series: OHLCVSeries,
    thresholds: QualityThresholds | None = None,
    *,
    dropped_fraction: float = 0.0,
    as_of: dt.date | None = None,
    calendar: EGXCalendar | None = None,
) -> QualityReport:
    """Run every gate over a series.

    `dropped_fraction` comes from `FetchResult` — bars Yahoo returned that had to
    be discarded. `as_of` is the date to measure last-bar staleness against.
    `calendar` lets the price-jump gate account for sessions missing from the feed;
    without it every bar step is treated as a single session, which over-reports.
    """
    t = thresholds or QualityThresholds()

    if not series.candles:
        return QualityReport(
            symbol=series.symbol,
            issues=[Issue(IssueCode.NO_DATA, Severity.ERROR, "no usable bars returned")],
        )

    return QualityReport(
        symbol=series.symbol,
        issues=[
            *_check_history_length(series, t),
            *_check_drop_rate(dropped_fraction, t),
            *_check_price_jumps(series, t, calendar),
            *_check_feed_metadata(series, t, as_of),
            *_check_dead_feed(series, t),
        ],
    )
