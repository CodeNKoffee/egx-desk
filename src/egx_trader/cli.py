"""Command line entry point.

Commands that only read static data (`universe`, `calendar`) deliberately avoid
`get_settings()`, so they work before `.env` is filled in. Anything touching the
network or the ledger loads settings and will refuse to run without them.
"""

from __future__ import annotations

import datetime as dt
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from egx_trader.config import UniverseMode, get_settings
from egx_trader.control.server import DeskServer
from egx_trader.dashboard import render_dashboard
from egx_trader.data.intraday.recorder import IntradayRecorder
from egx_trader.data.intraday.sources.base import TickSourceError
from egx_trader.data.intraday.sources.thndrx import ThndrXSource
from egx_trader.data.intraday.store import IntradayStore
from egx_trader.data.providers import ProviderError, build_providers
from egx_trader.data.quality import QualityReport, check_series
from egx_trader.data.yahoo import ChartRange, MarketDataError, YahooClient
from egx_trader.market_calendar import CAIRO, CalendarCoverageError, EGXCalendar
from egx_trader.market_calendar.verify import (
    CLOSED_THRESHOLD,
    OPEN_THRESHOLD,
    build_evidence,
    collect_presence,
    label_for,
)
from egx_trader.notify.desktop import needs_you
from egx_trader.portfolio.positions import load_positions
from egx_trader.universe import InstrumentRegistry, ShariaStatus, symbol_code

app = typer.Typer(
    help="EGX momentum/breakout trading system.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_SHARIA_STYLE = {
    ShariaStatus.COMPLIANT: "green",
    ShariaStatus.NON_COMPLIANT: "red",
    ShariaStatus.UNKNOWN: "yellow",
}


@app.command()
def universe(
    mode: Annotated[
        UniverseMode, typer.Option(help="Which universe to resolve.")
    ] = UniverseMode.SHARIA,
    show_all: Annotated[
        bool, typer.Option("--all", help="List every instrument, not just the selected universe.")
    ] = False,
) -> None:
    """List the instrument master and its coverage gaps."""
    registry = InstrumentRegistry.load()
    instruments = registry.all() if show_all else registry.universe(mode)

    table = Table(
        title=f"EGX instruments — {'all records' if show_all else f'{mode.value} universe'}"
    )
    table.add_column("Symbol", style="bold")
    table.add_column("Name")
    table.add_column("Sharia")
    table.add_column("Status")
    table.add_column("Weight", justify="right")
    table.add_column("T+0", justify="center")

    for instrument in sorted(instruments, key=lambda i: i.symbol):
        weight = f"{instrument.index_weight:.2f}" if instrument.index_weight is not None else "—"
        table.add_row(
            instrument.symbol,
            instrument.name_en[:44],
            f"[{_SHARIA_STYLE[instrument.sharia]}]{instrument.sharia.value}[/]",
            instrument.status.value,
            weight,
            "✓" if instrument.t0_eligible else "—",
        )

    console.print(table)
    console.print(f"\n[bold]{len(instruments)}[/] instruments listed\n")

    console.print("[bold]Coverage[/]")
    for line in registry.coverage().summary_lines():
        console.print(f"  {line}")
    console.print(
        "\n[dim]Missing fields are genuinely unsourced, not defaults. `board` gates T+0 "
        "eligibility, so until it is populated nothing is treated as T+0 tradable.[/]"
    )


@app.command()
def calendar(
    date: Annotated[
        str | None, typer.Option(help="Date to inspect, YYYY-MM-DD. Defaults to today.")
    ] = None,
    days: Annotated[int, typer.Option(help="How many days forward to show.")] = 14,
) -> None:
    """Show trading days, session phases and T+2 settlement dates."""
    cal = EGXCalendar(strict=False)
    start = dt.date.fromisoformat(date) if date else dt.datetime.now(CAIRO).date()

    now = dt.datetime.now(CAIRO)
    console.print(f"\n[bold]Now (Cairo):[/] {now:%Y-%m-%d %H:%M} — phase: ", end="")
    phase = cal.session_phase(now)
    console.print(f"[bold]{phase.value}[/]" + ("" if phase.accepts_orders else " [dim](closed)[/]"))

    strict = EGXCalendar()
    console.print(f"[bold]Calendar verified through:[/] {strict.verified_through}")
    try:
        strict.is_trading_day(start + dt.timedelta(days=days))
    except CalendarCoverageError:
        console.print(
            "[yellow]Part of this range is past verified coverage — shown non-strict. "
            "Live trading will refuse these dates until holidays.yaml is extended.[/]"
        )

    table = Table(title=f"EGX sessions from {start}")
    table.add_column("Date", style="bold")
    table.add_column("Day")
    table.add_column("Trading")
    table.add_column("Note")
    table.add_column("T+2 settles", justify="right")

    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        trading = cal.is_trading_day(day)
        holiday = cal.holiday_name(day)
        note = holiday or ("weekend" if not trading else "")
        settles = f"{cal.add_sessions(day, 2)}" if trading else "—"
        table.add_row(
            f"{day}",
            f"{day:%a}",
            "[green]open[/]" if trading else "[dim]closed[/]",
            note,
            settles,
        )

    console.print(table)


@app.command()
def data(
    symbols: Annotated[
        list[str] | None,
        typer.Argument(help="Symbols to fetch. Defaults to your current holdings."),
    ] = None,
    chart_range: Annotated[
        ChartRange, typer.Option("--range", help="History depth.")
    ] = ChartRange.YEAR_10,
) -> None:
    """Fetch daily OHLCV and report data quality.

    Needs EGX_PROXY_KEY in .env — the Cloudflare Worker rejects unkeyed requests.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    targets = symbols or ["BIOC.CA", "GTWL.CA", "AMOC.CA", "AFMC.CA"]
    cal = EGXCalendar(strict=False)
    registry = InstrumentRegistry.load()
    today = dt.datetime.now(CAIRO).date()

    with YahooClient(settings.yahoo_base_url, settings.proxy_key) as client:
        for raw in targets:
            instrument = registry.get(raw)
            name = instrument.name_en if instrument else "(not in master)"
            try:
                result = client.fetch_daily(raw, chart_range)
            except MarketDataError as exc:
                console.print(f"[red]{raw:9}[/] {type(exc).__name__}: {exc}\n")
                continue

            series = result.series
            report = check_series(
                series,
                dropped_fraction=result.egx_drop_fraction(cal),
                as_of=today,
                calendar=cal,
            )
            last = series.candles[-1]
            verdict = "[green]USABLE[/]" if report.is_usable else "[red]BLOCKED[/]"

            console.print(f"[bold]{series.symbol}[/] {name}  {verdict}")
            console.print(
                f"  {len(series)} bars  {series.first_date} → {series.last_date}   "
                f"gaps: raw {result.dropped_fraction:.0%}, "
                f"EGX-adjusted {result.egx_drop_fraction(cal):.1%}, "
                f"trailing placeholders trimmed: {len(result.synthetic_dates)}"
            )
            console.print(
                f"  last session {last.date}: O {last.open:g}  H {last.high:g}  "
                f"L {last.low:g}  C {last.close:g}  vol {last.volume:,}  "
                f"turnover {last.turnover:,.0f} EGP"
            )
            if series.splits:
                console.print(
                    "  splits: " + ", ".join(f"{s.date} {s.ratio:g}x" for s in series.splits[-4:])
                )
            for issue in report.issues[:5]:
                colour = "red" if issue.severity.value == "error" else "yellow"
                console.print(f"  [{colour}]{issue}[/]")
            if len(report.issues) > 5:
                console.print(f"  [dim]… {len(report.issues) - 5} more[/]")
            console.print()


@app.command()
def audit(
    mode: Annotated[
        UniverseMode, typer.Option(help="Which universe to audit.")
    ] = UniverseMode.SHARIA,
    fail_under: Annotated[
        float,
        typer.Option(help="Exit non-zero if fewer than this percent of names pass the gates."),
    ] = 0.0,
    workers: Annotated[int, typer.Option(help="Concurrent fetches.")] = 6,
) -> None:
    """Run the data-quality gates across the whole universe.

    This is how you find out whether the feed is good enough to trade on. It
    places no orders and needs no broker credentials.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    cal = EGXCalendar(strict=False)
    registry = InstrumentRegistry.load()
    symbols = registry.symbols(mode)
    today = dt.datetime.now(CAIRO).date()

    def audit_one(symbol: str) -> tuple[str, QualityReport | None, str | None]:
        with YahooClient(settings.yahoo_base_url, settings.proxy_key) as client:
            try:
                fetched = client.fetch_daily(symbol, ChartRange.YEAR_10)
            except MarketDataError as exc:
                return symbol, None, type(exc).__name__
        return (
            symbol,
            check_series(
                fetched.series,
                dropped_fraction=fetched.egx_drop_fraction(cal),
                as_of=today,
                calendar=cal,
            ),
            None,
        )

    console.print(f"Auditing {len(symbols)} names in the [bold]{mode.value}[/] universe…\n")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(audit_one, symbols))

    usable = [r for r in results if r[1] and r[1].is_usable]
    blocked = [r for r in results if r[1] and not r[1].is_usable]
    unfetchable = [r for r in results if r[1] is None]
    pass_rate = len(usable) / len(symbols) * 100 if symbols else 0.0

    table = Table(title="Data quality")
    table.add_column("Outcome", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Share", justify="right")
    for label, group, colour in (
        ("passing", usable, "green"),
        ("blocked", blocked, "red"),
        ("unfetchable", unfetchable, "yellow"),
    ):
        share = len(group) / len(symbols) * 100 if symbols else 0
        table.add_row(f"[{colour}]{label}[/]", str(len(group)), f"{share:.0f}%")
    console.print(table)

    reasons: Counter[str] = Counter()
    for _, report, _ in results:
        if report:
            for issue in report.errors:
                reasons[issue.code.value] += 1
    if reasons:
        console.print("\n[bold]Blocking issues[/]")
        for code, count in reasons.most_common():
            console.print(f"  {code:<26} {count}")

    if unfetchable:
        console.print(
            "\n[yellow]Absent upstream:[/] " + ", ".join(symbol_code(s) for s, _, _ in unfetchable)
        )

    console.print(f"\n[bold]{pass_rate:.0f}%[/] of the universe is usable.")
    if pass_rate < fail_under:
        console.print(
            f"[red]Below the {fail_under:.0f}% floor.[/] The feed is not good enough "
            "to trade on — see task #5 on sourcing a second data provider."
        )
        raise typer.Exit(1)


@app.command(name="verify-calendar")
def verify_calendar(
    years: Annotated[int, typer.Option(help="How much history to sample.")] = 5,
    write: Annotated[
        bool, typer.Option("--write", help="Rewrite holidays.yaml from the evidence.")
    ] = False,
) -> None:
    """Check holidays.yaml against the exchange's actual trading record.

    Egypt's public holidays are not EGX's holidays — EGX traded on Police Day and
    June 30 in 2026 but was closed on both in 2024, and the Islamic and Coptic
    observances move every year. So the calendar is derived from evidence: a date
    is closed when almost none of a basket of liquid names has a bar for it.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    registry = InstrumentRegistry.load()
    basket = [i.symbol for i in registry.universe(UniverseMode.SHARIA) if i.index_weight]
    chart_range = ChartRange.YEAR_5 if years > 2 else ChartRange.YEAR_2

    def dates_for(symbol: str) -> set[dt.date] | None:
        with YahooClient(settings.yahoo_base_url, settings.proxy_key) as client:
            try:
                fetched = client.fetch_daily(symbol, chart_range)
            except MarketDataError:
                return None
        return {candle.date for candle in fetched.series.candles}

    console.print(f"Sampling {len(basket)} index-weighted names over {chart_range.value}…\n")
    presence, basket_size = collect_presence(basket, dates_for)
    if not basket_size:
        console.print("[red]No symbols returned data — cannot verify.[/]")
        raise typer.Exit(1)

    end = max(presence)
    start = max(min(presence), end - dt.timedelta(days=365 * years))
    evidence = build_evidence(presence, basket_size, EGXCalendar(strict=False), start, end)

    console.print(f"[bold]Basket:[/] {basket_size} symbols   [bold]Range:[/] {start} → {end}")
    console.print(
        f"[dim]closed if <{CLOSED_THRESHOLD:.0%} of the basket traded; "
        f"false holiday if >{OPEN_THRESHOLD:.0%} did[/]\n"
    )

    console.print(f"[bold]{len(evidence.closed)}[/] non-trading weekdays derived")
    if evidence.false_holidays:
        console.print(
            f"\n[red]{len(evidence.false_holidays)} false holidays[/] "
            "— marked closed, but the market traded:"
        )
        for day, fraction in evidence.false_holidays:
            console.print(f"  {day} {day:%a}  {fraction:.0%} of the basket traded")
    else:
        console.print("\n[green]No false holidays.[/]")

    if evidence.ambiguous:
        console.print(
            f"\n[yellow]{len(evidence.ambiguous)} ambiguous[/] "
            "— between thresholds, left alone rather than guessed:"
        )
        for day, fraction in evidence.ambiguous[:8]:
            console.print(f"  {day} {day:%a}  {fraction:.0%}")

    if not write:
        console.print("\n[dim]Re-run with --write to rebuild holidays.yaml.[/]")
        return

    path = Path(__file__).parent / "market_calendar" / "holidays.yaml"
    existing = path.read_text()
    header = existing.split("verified_through:")[0]
    body = "\n".join(f"  {day}: {label_for(day)}" for day in evidence.closed)
    footer = existing[existing.index("\n# Sessions that run on non-default hours") :]
    path.write_text(f"{header}verified_through: {end}\n\nholidays:\n{body}\n{footer}")
    console.print(f"\n[green]Wrote[/] {len(evidence.closed)} dates, verified_through={end}")


@app.command(name="providers")
def providers_cmd() -> None:
    """Show which data providers are configured and in what priority order."""
    try:
        settings = get_settings()
        built = build_providers(settings)
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    table = Table(title="Data providers")
    table.add_column("#", justify="right")
    table.add_column("Provider", style="bold")
    table.add_column("Configured")
    table.add_column("Cost")
    table.add_column("Notes")

    facts = {
        "yahoo": ("free", "automatable; drops 22-30% of EGX sessions at random"),
        "eodhd": ("$19.99/mo", "API-native; EGX coverage unverified until tested"),
        "tradingview_csv": (
            "$28.29/mo",
            "manual export only — no data API at any tier, ToS forbids automation",
        ),
    }
    for index, provider in enumerate(built, start=1):
        cost, note = facts.get(provider.name, ("—", ""))
        ready = provider.is_configured()
        table.add_row(
            str(index),
            provider.name,
            "[green]yes[/]" if ready else "[yellow]no[/]",
            cost,
            note,
        )
    console.print(table)
    console.print(
        "\n[dim]Order is priority: the first provider with a bar for a date wins, "
        "later ones only fill gaps. Set with EGX_DATA_PROVIDERS.[/]"
    )


@app.command(name="compare-providers")
def compare_providers(
    symbols: Annotated[
        list[str] | None, typer.Argument(help="Symbols to compare. Defaults to your holdings.")
    ] = None,
) -> None:
    """Measure configured providers against each other on missing EGX sessions.

    This is the test that decides whether paying for a feed is worth it. It counts
    the sessions each provider actually covers, so "unverified" becomes a number.
    """
    try:
        settings = get_settings()
        built = build_providers(settings)
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    ready = [p for p in built if p.is_configured()]
    if len(ready) < 2:
        console.print(
            f"[yellow]Only {len(ready)} provider configured.[/] Comparison needs at least "
            "two — set EGX_EODHD_API_KEY, or drop TradingView exports into "
            f"{settings.tv_csv_dir}, then add them to EGX_DATA_PROVIDERS."
        )
        if not ready:
            raise typer.Exit(1)

    cal = EGXCalendar(strict=False)
    targets = symbols or ["BIOC.CA", "GTWL.CA", "AMOC.CA", "AFMC.CA"]

    table = Table(title="Provider coverage — EGX sessions actually present")
    table.add_column("Symbol", style="bold")
    for provider in ready:
        table.add_column(provider.name, justify="right")
    table.add_column("merged", justify="right", style="bold green")

    for raw in targets:
        row = [raw]
        per_provider: dict[str, set[dt.date]] = {}
        for provider in ready:
            try:
                per_provider[provider.name] = provider.fetch_daily(raw).dates
            except ProviderError:
                per_provider[provider.name] = set()

        union = set().union(*per_provider.values()) if per_provider else set()
        if not union:
            table.add_row(raw, *["—"] * (len(ready) + 1))
            continue

        first, last = min(union), max(union)
        expected = len(cal.trading_days_in(first, last))
        for provider in ready:
            have = len(per_provider[provider.name])
            row.append(f"{have}/{expected} ({have / expected:.0%})" if expected else str(have))
        row.append(f"{len(union)}/{expected} ({len(union) / expected:.0%})" if expected else "—")
        table.add_row(*row)

    console.print(table)
    console.print(
        "\n[dim]`merged` is what EGX_DATA_PROVIDERS gives you with all of them stacked. "
        "If it is not meaningfully above the free provider alone, the paid feed is not "
        "solving the problem.[/]"
    )


@app.command()
def dashboard(
    output: Annotated[Path | None, typer.Option("--out", help="Where to write the HTML.")] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open it after writing.")
    ] = True,
) -> None:
    """Render the desk dashboard to a self-contained HTML file.

    Everything is inlined, so the page works offline and has no order path.
    Re-run to refresh — it is a snapshot, not a live feed.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    path = render_dashboard(settings, out_path=output)
    console.print(f"[green]Wrote[/] {path}  ({path.stat().st_size / 1024:.0f} KB)")
    console.print(
        "[dim]Drag panel headers to move, corner grips to resize, and the layout "
        "buttons to switch arrangement. Your changes persist per layout.[/]"
    )
    if open_browser:
        webbrowser.open(path.resolve().as_uri())


@app.command(name="thndrx-login")
def thndrx_login(
    timeout_minutes: Annotated[int, typer.Option(help="How long to wait for you.")] = 10,
) -> None:
    """Open a browser so you can log into ThndrX, and save the session.

    ThndrX authenticates by scanning a QR code in the Thndr phone app — there is no
    headless path to that, by design. This opens a real window, waits while you scan,
    then persists the session so the recorder can reuse it.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    state = settings.thndrx_profile_dir
    console.print(
        f"\nOpening ThndrX. Scan the QR code with the Thndr app, then wait here — "
        f"the session saves itself.\n[dim]Timeout {timeout_minutes} min. "
        f"Session file: {state}[/]\n"
    )
    needs_you(
        "ThndrX login",
        "Browser is open — scan the QR code in the Thndr app.",
        enabled=settings.macos_notifications,
    )

    try:
        ok = ThndrXSource.login(state, settings.thndrx_login_url, timeout_minutes=timeout_minutes)
    except TickSourceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    if not ok:
        console.print(
            "[yellow]Not signed in[/] — the browser closed, or the timeout passed "
            "before login completed. Nothing was saved."
        )
        console.print(
            "[dim]Success is detected by leaving /auth for the app itself, so a "
            "half-finished login never writes a session file.[/]"
        )
        raise typer.Exit(1)
    console.print(f"[green]Signed in.[/] Profile kept at {state}")
    console.print(
        "[dim]The browser closing is expected — the profile is saved. It will expire "
        "eventually; the recorder stops and tells you when.[/]"
    )


@app.command(name="record")
def record(
    symbols: Annotated[
        list[str] | None, typer.Argument(help="Symbols to record. Defaults to your holdings.")
    ] = None,
    poll_seconds: Annotated[int, typer.Option(help="Seconds between readings.")] = 20,
) -> None:
    """Record intraday bars for the rest of the session.

    Nothing sells EGX intraday data, so a session that passes unrecorded is gone.
    The loop survives transient failures for that reason; an expired login is the
    one thing it stops for, because retrying cannot fix it.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    cal = EGXCalendar(strict=False)
    now = dt.datetime.now(CAIRO)
    if not cal.is_open(now):
        console.print(
            f"[yellow]Market is closed[/] ({cal.session_phase(now).value}). "
            f"Next session: {cal.next_session(now.date())}."
        )
        raise typer.Exit(0)

    targets = symbols or [p.symbol for p in load_positions(settings.positions_path)]
    if not targets:
        console.print("[red]No symbols.[/] Pass them, or add holdings to data/positions.yaml.")
        raise typer.Exit(1)

    state = settings.thndrx_profile_dir
    source = ThndrXSource(state)
    if not source.has_saved_session():
        console.print("[red]No saved ThndrX session.[/] Run [bold]egx thndrx-login[/] first.")
        needs_you(
            "Recorder blocked",
            "No ThndrX session — run `egx thndrx-login`.",
            enabled=settings.macos_notifications,
        )
        raise typer.Exit(1)

    console.print(f"Recording {len(targets)} symbols every {poll_seconds}s until the close…")
    with IntradayStore(settings.intraday_db_path) as store:
        recorder = IntradayRecorder(source, store, targets, poll_seconds=poll_seconds, calendar=cal)
        try:
            source.start()
            stats = recorder.run()
        finally:
            source.close()

    console.print(f"\n[bold]Stopped:[/] {stats.stopped_reason}")
    console.print(f"  {stats.summary()}")
    if stats.stopped_reason and "needs a human" in stats.stopped_reason:
        needs_you(
            "ThndrX session expired",
            "Recording stopped mid-session. Run `egx thndrx-login` to resume.",
            enabled=settings.macos_notifications,
        )


@app.command()
def desk(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1. 0 picks a free one.")] = 0,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Open the live desk — the dashboard with working controls.

    Starts a local server bound to 127.0.0.1 only, never the network: this process
    can open a browser holding a live broker session. Every request carries a
    token generated fresh for this run, because localhost is not a security
    boundary — any process or page on the machine can reach it.

    The desk starts and stops jobs. It has no order path, deliberately: a button
    is one mis-click from a trade, so order flow stays behind the execution-mode
    gates and per-order confirmation instead.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(1) from exc

    page = settings.data_dir / "desk.html"
    server = DeskServer(settings, page, port=port)
    render_dashboard(settings, out_path=page, desk_token=server.token)
    server.start()

    console.print(f"\n[bold green]Desk running[/] on 127.0.0.1:{server.port}")
    console.print(f"[dim]{server.url}[/]")
    console.print(
        "\n[dim]Bound to localhost only. Token-gated. No order path — the desk "
        "starts jobs, it does not place trades.[/]"
    )
    console.print("[dim]Ctrl-C to stop.[/]\n")
    if open_browser:
        webbrowser.open(server.url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\nStopping…")
    finally:
        server.stop()


if __name__ == "__main__":
    app()
