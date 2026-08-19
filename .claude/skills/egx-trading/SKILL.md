---
name: egx-trading
description: Domain rules and hard-won gotchas for building on the Egyptian Exchange (EGX) through Thndr — market microstructure, settlement, data-source traps, and this project's safety invariants. Load when working on egx-trader, or on any EGX market data, backtest, strategy, order-routing, calendar or execution code. Also load before changing risk limits, execution modes, or anything that could place an order.
---

# Building on EGX

Everything here was established by measurement against the live market, not from
documentation. Each item exists because getting it wrong produced a real bug.

## Market facts

- **Sessions run Sunday–Thursday.** Friday and Saturday are the weekend. Code that
  assumes Mon–Fri is wrong in a way that silently corrupts settlement dates.
- **Phases (Africa/Cairo):** pre-open 09:30–10:00 with a *random* close between
  09:50 and 10:00, continuous 10:00–14:15, closing auction 14:15–14:25,
  trading-at-close 14:25–14:30. An order rejected in the random-close window is
  expected behaviour, not a bug.
- **Daily price band is about ±20%** on the most-active board, tighter elsewhere,
  with a ±10% MVWAP circuit breaker halting a stock for 10 minutes. The band is
  visibly binding: BIOC printed exactly −20.0% then +20.0% on consecutive sessions
  in 2026. **An order cannot fill through a limit-up.**
- **Settlement is T+2 by default.** A lot bought Sunday is not sellable until
  Tuesday. T+0 needs all three of: an eligible board (Most Active / Moderate /
  Tamayuz), a Thndr Trader subscription, *and* routing through the advanced limit
  screen — which will not accept a market order.
- **Egypt's public holidays are NOT the exchange's holidays.** EGX traded on Police
  Day and June 30 in 2026 but was closed on both in 2024. Islamic and Coptic dates
  move annually. Never derive the calendar from a holiday list; derive it from the
  trading record (`egx verify-calendar`) — a date is closed when almost none of a
  liquid basket has a bar for it.

## Thndr specifics

- **50 commission-free executions per month**, then 2 EGP + 0.1%. Trade count is a
  budget, not just a cost line.
- **Stop orders trigger a MARKET sell.** Model the fill at the next available price
  after trigger, never cleanly at the stop — that is the most common way a backtest
  flatters itself, and it matters most on gaps, which is exactly when stops fire.
- Stops rest at the broker, so they protect a position even when this software is
  not running. That is the single most valuable thing the subscription buys.
- **No public API.** Execution means driving the web session, which carries
  account-suspension risk.

## Data source traps

Every one of these was found by running code against the live feed.

- **Yahoo `range=max` returns MONTHLY bars** while still reporting `interval=1d`.
  BIOC: `10y` → 2089 bars at 1-day spacing, `max` → 275 at 31-day spacing. Always
  measure the spacing you actually received.
- **Consecutive bars are not consecutive sessions.** Yahoo drops 22–30% of EGX
  sessions at random. A bar step can span several days, so a price-band check must
  compound its allowance over sessions actually elapsed or it flags hundreds of
  false positives.
- **Yahoo pads series with a flat, zero-volume trailing bar** carrying a stale
  price that disagrees with the broker. Trim it. But keep *mid-series* flat bars —
  they are genuine no-trade sessions, and dropping them compresses the time axis so
  a "55-day" channel silently spans far more than 55 sessions.
- **`adj_close` does not fix EGX corporate actions** — identical jump counts to raw
  `close`.
- **Yahoo has no intraday for `.CA` at all.** It downgrades any interval to `1d`.
  Intraday must be recorded, never fetched.
- **No single feed is sufficient.** Yahoo 81%, EODHD 92%, merged **99%** — they miss
  different sessions. Merge with per-bar provenance, because the feeds disagree on
  prices and a backtest that cannot say which vendor supplied a bar cannot tell an
  edge from an artefact.
- **TradingView has no data API at any tier** and forbids automated collection.
  Even the CDP/MCP bridge approach has `exportData()` blocked. Manual CSV export
  only.

## Project invariants

Break these and the system becomes unsafe rather than merely wrong.

- **Unknown never reads as permissive.** No board data → not T+0 eligible. No
  sourced Sharia label → excluded from the Sharia universe. An unsourced label is
  not a label.
- **Nothing may block an exit.** Every risk gate is an *entry* gate. A cap that
  blocks a sell does not reduce risk, it creates it — you hold a position the
  software refuses to release. Exits face settlement alone, and that is the
  broker's rule anyway. Protection comes from broker-resting stops.
- **Expiry never means execute.** An unconfirmed ticket is discarded. "No
  objection, so proceed" turns being away from the desk into a trading decision.
- **Idempotency is keyed to the day**, not the timestamp. Two identical tickets
  minutes apart are the same intent; different ids let a retry become a second
  position.
- **A missing indicator is missing**, never zero and never forward-filled. Warm-up
  periods must not trade.
- **RSI means Wilder's smoothing.** An SMA of gains and losses is a different
  indicator: after 20 down bars then 16 up, the SMA version reads 100 while Wilder
  reads 69, so a "sell above 70" rule fires on one and not the other.
- **Auto mode is double-gated** (`EGX_EXECUTION_MODE=auto` *and*
  `EGX_I_UNDERSTAND_LIVE_TRADING=true`) so it cannot be reached by one typo.

## Backtesting rules

- No look-ahead, structurally: a strategy sees bars 0..i, the fill is at the open
  of i+1. Do not ask a strategy to behave; never hand it the next bar.
- Cap fills at a few percent of the day's real volume. Many EGX names trade under
  500k EGP a day, where a market order walks the book a long way.
- Liquidity is measured in **EGP turnover**, not share count. A million shares at
  2 EGP and a thousand at 500 EGP are not comparable positions.
- Model the full cost stack: the 50-free-trade allowance per calendar month, 10%
  CGT on realised gains, the 245 EGP/month subscription, T+2 exit delays.
- A result on one symbol is a **case study, not evidence.** Say so.

## Things that are not this system's job

- Placing orders from a dashboard. The desk starts and stops jobs; order flow stays
  behind execution-mode gates and per-order confirmation.
- Certifying Sharia compliance. Labels are aggregated from Thndr, never derived.
- Giving investment advice. Strategy parameters and capital decisions belong to the
  operator.
