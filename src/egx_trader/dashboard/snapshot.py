"""Build the JSON snapshot the dashboard renders.

Everything the UI shows is computed here, in Python, and embedded into a single
self-contained HTML file. No server, no CDN, no network at view time — the page
opens from disk and works offline.

The snapshot is deliberately honest about what is not known yet. A dashboard that
renders empty panels as though they were zeroes would be worse than no dashboard:
this system has no ledger, no backtest and no signals, and the UI says so.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

from egx_trader.config import ExecutionMode, Settings, UniverseMode
from egx_trader.market_calendar import CAIRO, CalendarCoverageError, EGXCalendar
from egx_trader.portfolio.positions import load_positions
from egx_trader.universe import InstrumentRegistry

# Phase status is stated rather than inferred, so the UI cannot imply a capability
# that does not exist. Update these as phases land.
PHASES: list[dict[str, str]] = [
    {
        "n": "0",
        "name": "Foundations",
        "detail": "config, calendar, universe, data",
        "state": "done",
    },
    {"n": "1", "name": "Strategy", "detail": "features, backtest, recorder", "state": "blocked"},
    {"n": "2", "name": "Paper", "detail": "ledger, risk engine, alerts", "state": "todo"},
    {"n": "3", "name": "Assisted", "detail": "ThndrX adapter, broker stops", "state": "todo"},
    {"n": "4", "name": "Auto", "detail": "no human in the loop", "state": "todo"},
]

PROVIDER_FACTS: dict[str, dict[str, str]] = {
    "yahoo": {
        "cost": "free",
        "automatable": "yes",
        "note": "Loses 22-30% of EGX sessions at random, spread evenly Sun-Thu.",
    },
    "eodhd": {
        "cost": "$19.99/mo",
        "automatable": "yes",
        "note": "API-native, 241+ EGX tickers. Coverage unverified — their demo "
        "token cannot reach EGX. Run compare-providers before paying.",
    },
    "tradingview_csv": {
        "cost": "$28.29/mo",
        "automatable": "no",
        "note": "No data API at any tier; ToS forbids automated collection and "
        "licenses data display-only. Manual 'Export chart data' CSV only.",
    },
}


@dataclass
class Snapshot:
    generated_at: str
    session: dict[str, Any]
    universe: dict[str, Any]
    calendar: dict[str, Any]
    providers: list[dict[str, Any]]
    safety: dict[str, Any]
    holdings: list[dict[str, Any]]
    phases: list[dict[str, str]] = field(default_factory=lambda: PHASES)
    notices: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_snapshot(settings: Settings, registry: InstrumentRegistry) -> Snapshot:
    now = dt.datetime.now(CAIRO)
    loose = EGXCalendar(strict=False)
    strict = EGXCalendar()

    phase = loose.session_phase(now)
    today = now.date()

    upcoming: list[dict[str, Any]] = []
    for offset in range(14):
        day = today + dt.timedelta(days=offset)
        trading = loose.is_trading_day(day)
        upcoming.append(
            {
                "date": day.isoformat(),
                "dow": day.strftime("%a"),
                "trading": trading,
                "note": loose.holiday_name(day) or ("weekend" if not trading else ""),
                "settles": loose.add_sessions(day, 2).isoformat() if trading else None,
            }
        )

    try:
        strict.is_trading_day(today)
        coverage_ok = True
    except CalendarCoverageError:
        coverage_ok = False

    coverage = registry.coverage()
    provider_names = [p.strip() for p in settings.data_providers.split(",") if p.strip()]
    configured = {
        "yahoo": bool(settings.proxy_key),
        "eodhd": bool(settings.eodhd_api_key),
        "tradingview_csv": settings.tv_csv_dir.is_dir() and any(settings.tv_csv_dir.glob("*.csv")),
    }

    providers = [
        {
            "name": name,
            "priority": index,
            "active": name in provider_names,
            "configured": configured.get(name, False),
            **PROVIDER_FACTS.get(name, {"cost": "—", "automatable": "?", "note": ""}),
        }
        for index, name in enumerate(
            provider_names + [n for n in PROVIDER_FACTS if n not in provider_names], start=1
        )
    ]

    # Read from a gitignored file the operator owns. Never hardcoded: quantities
    # and cost basis are personal financial data and do not belong in a repo.
    holdings = []
    for position in load_positions(settings.positions_path):
        instrument = registry.get(position.symbol)
        holdings.append(
            {
                "symbol": position.symbol,
                "qty": position.qty,
                "avg_cost": position.avg_cost,
                "name": instrument.name_en if instrument else "unknown",
                "sharia": instrument.sharia.value if instrument else "unknown",
                "book_cost": position.book_cost,
            }
        )

    notices: list[dict[str, str]] = []
    if not coverage_ok:
        notices.append(
            {
                "level": "warn",
                "text": f"Calendar verified only to {strict.verified_through}. It is derived "
                "from the trading record, which trails the feed by a few sessions. Fine "
                "for backtesting; live trading needs EGX's published forward calendar.",
            }
        )
    if not holdings:
        notices.append(
            {
                "level": "warn",
                "text": "No positions loaded. Copy positions.example.yaml to "
                "data/positions.yaml and enter your holdings there — that path is "
                "gitignored, so real quantities never reach the repository.",
            }
        )
    notices.append(
        {
            "level": "warn",
            "text": "Only 19% of the Sharia universe passes the data-quality gates on Yahoo. "
            "A backtest built on that would produce a number worth nothing. This is the "
            "blocker on Phase 1.",
        }
    )
    if settings.execution_mode is ExecutionMode.ALERT:
        notices.append(
            {
                "level": "ok",
                "text": "Execution mode is 'alert' — the system emits tickets and never "
                "touches a browser. Nothing here can place an order.",
            }
        )

    return Snapshot(
        generated_at=now.isoformat(timespec="seconds"),
        session={
            "now_cairo": now.strftime("%Y-%m-%d %H:%M"),
            "phase": phase.value,
            "accepts_orders": phase.accepts_orders,
            "is_trading_day": loose.is_trading_day(today),
            "next_session": loose.next_session(today).isoformat(),
            "in_random_close": loose.in_pre_open_random_close(now),
        },
        universe={
            "mode": settings.universe_mode.value,
            "sharia_count": len(registry.universe(UniverseMode.SHARIA)),
            "all_count": len(registry.universe(UniverseMode.ALL)),
            "total": coverage.total,
            "by_sharia": {k.value: v for k, v in coverage.by_sharia.items()},
            "by_status": {k.value: v for k, v in coverage.by_status.items()},
            "missing_fields": coverage.missing_by_field,
            "t0_eligible": coverage.t0_eligible,
            "instruments": [
                {
                    "symbol": i.symbol,
                    "name": i.name_en,
                    "sharia": i.sharia.value,
                    "weight": i.index_weight,
                    "t0": i.t0_eligible,
                }
                for i in sorted(registry.universe(settings.universe_mode), key=lambda x: x.symbol)
            ],
        },
        calendar={
            "verified_through": strict.verified_through.isoformat(),
            "coverage_ok": coverage_ok,
            "holiday_count": strict.holiday_count,
            "upcoming": upcoming,
        },
        providers=providers,
        safety={
            "execution_mode": settings.execution_mode.value,
            "live_trading_ack": settings.i_understand_live_trading,
            "max_order_egp": settings.max_order_egp,
            "confirm_timeout_s": settings.confirm_timeout_seconds,
            "free_trades": settings.free_trades_per_month,
            "max_position_pct": settings.max_position_pct,
            "daily_loss_limit_pct": settings.daily_loss_limit_pct,
            "grandfather_existing": settings.grandfather_existing,
        },
        holdings=holdings,
        notices=notices,
    )
