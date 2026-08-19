"""Intraday aggregation tests.

Intraday EGX data cannot be re-fetched — Yahoo has none, so a session missed is
gone. The rules that protect against silently fabricating it get the attention:
bars never span the close, and an empty period yields no bar at all.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest

from egx_trader.data.intraday.aggregator import aggregate, bucket_start, merge_bars
from egx_trader.data.intraday.models import BarInterval, IntradayBar, Tick
from egx_trader.data.intraday.recorder import IntradayRecorder
from egx_trader.data.intraday.sources.base import SessionExpiredError, TickSourceError
from egx_trader.data.intraday.sources.thndrx import is_signed_in_url
from egx_trader.data.intraday.store import IntradayStore
from egx_trader.market_calendar import CAIRO

DAY = dt.date(2026, 8, 3)  # a Monday inside verified calendar coverage


def at(hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss, tzinfo=CAIRO)


def tick(hh: int, mm: int, price: float, ss: int = 0, vol: int | None = None) -> Tick:
    return Tick(symbol="BIOC.CA", at=at(hh, mm, ss), price=price, cumulative_volume=vol)


class TestBucketing:
    @pytest.mark.parametrize(
        "hh,mm,ss,interval,expect",
        [
            (10, 0, 0, BarInterval.M1, (10, 0)),
            (10, 0, 59, BarInterval.M1, (10, 0)),
            (10, 1, 0, BarInterval.M1, (10, 1)),
            (10, 3, 30, BarInterval.M5, (10, 0)),
            (10, 7, 1, BarInterval.M5, (10, 5)),
            (10, 44, 59, BarInterval.M15, (10, 30)),
        ],
    )
    def test_floors_to_the_bucket(
        self, hh: int, mm: int, ss: int, interval: BarInterval, expect: tuple[int, int]
    ) -> None:
        got = bucket_start(at(hh, mm, ss), interval)
        assert (got.hour, got.minute) == expect

    def test_buckets_in_cairo_not_utc(self) -> None:
        """Bar edges must line up with the session clock, which runs on Cairo minutes."""
        utc_noon = dt.datetime(2026, 8, 3, 9, 3, tzinfo=dt.UTC)  # 12:03 Cairo
        got = bucket_start(utc_noon, BarInterval.M5)
        assert (got.hour, got.minute) == (12, 0)
        assert got.tzinfo is CAIRO


class TestAggregate:
    def test_builds_ohlc_from_ticks(self) -> None:
        bars = aggregate(
            [tick(11, 0, 100.0), tick(11, 0, 105.0, ss=20), tick(11, 0, 98.0, ss=40)],
            BarInterval.M1,
        )
        assert len(bars) == 1
        bar = bars[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 98.0, 98.0)
        assert bar.tick_count == 3

    def test_splits_across_buckets(self) -> None:
        bars = aggregate([tick(11, 0, 100.0), tick(11, 1, 101.0)], BarInterval.M1)
        assert [b.start.minute for b in bars] == [0, 1]

    def test_out_of_order_ticks_are_sorted(self) -> None:
        bars = aggregate([tick(11, 1, 101.0), tick(11, 0, 100.0)], BarInterval.M1)
        assert [b.start.minute for b in bars] == [0, 1]

    def test_a_period_with_no_ticks_produces_no_bar(self) -> None:
        """Never zero-fill or forward-fill. Absence of data is not a price."""
        bars = aggregate([tick(11, 0, 100.0), tick(11, 5, 101.0)], BarInterval.M1)
        assert [b.start.minute for b in bars] == [0, 5]
        assert len(bars) == 2, "the four silent minutes must not become bars"

    def test_ticks_outside_the_session_are_dropped(self) -> None:
        """An out-of-hours quote is a stale screen value, not a trade."""
        bars = aggregate(
            [tick(8, 0, 100.0), tick(11, 0, 101.0), tick(16, 0, 102.0)], BarInterval.M1
        )
        assert len(bars) == 1
        assert bars[0].start.hour == 11

    def test_session_filter_can_be_disabled(self) -> None:
        bars = aggregate([tick(8, 0, 100.0)], BarInterval.M1, session_only=False)
        assert len(bars) == 1

    def test_bars_never_span_the_close(self) -> None:
        """14:29 and the next morning must not merge into one overnight range."""
        late = Tick(symbol="X.CA", at=at(14, 29), price=100.0)
        next_open = Tick(
            symbol="X.CA",
            at=dt.datetime(2026, 8, 4, 10, 0, tzinfo=CAIRO),
            price=140.0,
        )
        bars = aggregate([late, next_open], BarInterval.M15)
        assert len(bars) == 2
        assert all(b.high / b.low < 1.05 for b in bars), "no fabricated overnight range"

    def test_pre_open_ticks_are_kept(self) -> None:
        """Discovery from 09:30 is a real phase where orders rest."""
        assert len(aggregate([tick(9, 45, 100.0)], BarInterval.M1)) == 1

    def test_empty_input(self) -> None:
        assert aggregate([], BarInterval.M1) == []


class TestVolume:
    def test_differenced_from_the_running_total(self) -> None:
        bars = aggregate(
            [tick(11, 0, 100.0, vol=1000), tick(11, 0, 101.0, ss=30, vol=1500)],
            BarInterval.M1,
        )
        assert bars[0].volume == 500

    def test_a_single_tick_cannot_attribute_volume(self) -> None:
        """One observation gives no increment to measure, so claim nothing."""
        bars = aggregate([tick(11, 0, 100.0, vol=1000)], BarInterval.M1)
        assert bars[0].volume == 0
        assert bars[0].is_single_tick

    def test_a_counter_reset_yields_zero_not_a_negative(self) -> None:
        """Reconnects re-read the session total from scratch."""
        bars = aggregate(
            [tick(11, 0, 100.0, vol=5000), tick(11, 0, 101.0, ss=30, vol=200)],
            BarInterval.M1,
        )
        assert bars[0].volume == 0

    def test_missing_volume_is_tolerated(self) -> None:
        assert (
            aggregate([tick(11, 0, 100.0), tick(11, 0, 101.0, ss=5)], BarInterval.M1)[0].volume == 0
        )


class TestTickValidation:
    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Tick(symbol="X.CA", at=dt.datetime(2026, 8, 3, 11, 0), price=100.0)  # noqa: DTZ001

    def test_spread_needs_both_sides(self) -> None:
        base = {"symbol": "X.CA", "at": at(11, 0), "price": 100.0}
        assert Tick(**base).spread is None
        assert Tick(**base, bid=99.0, ask=101.0).spread == pytest.approx(2.0)


class TestMerge:
    def _bar(self, minute: int, close: float, ticks: int = 2) -> IntradayBar:
        return IntradayBar(
            symbol="BIOC.CA",
            interval=BarInterval.M1,
            start=at(11, minute),
            open=100.0,
            high=max(100.0, close),
            low=min(100.0, close),
            close=close,
            tick_count=ticks,
        )

    def test_a_rerecord_wins(self) -> None:
        """A later pass saw at least as many ticks, so it is the better record."""
        merged = merge_bars([self._bar(0, 100.0, 1)], [self._bar(0, 103.0, 9)])
        assert len(merged) == 1
        assert merged[0].tick_count == 9

    def test_disjoint_runs_combine_in_order(self) -> None:
        merged = merge_bars([self._bar(5, 105.0)], [self._bar(0, 100.0)])
        assert [b.start.minute for b in merged] == [0, 5]

    def test_merging_nothing_is_a_no_op(self) -> None:
        assert len(merge_bars([self._bar(0, 100.0)], [])) == 1


class TestBarValidation:
    def test_rejects_an_impossible_bar(self) -> None:
        with pytest.raises(ValueError, match="high"):
            IntradayBar(
                symbol="X.CA",
                interval=BarInterval.M1,
                start=at(11, 0),
                open=100.0,
                high=99.0,
                low=101.0,
                close=100.0,
                tick_count=1,
            )

    def test_end_follows_from_the_interval(self) -> None:
        bar = IntradayBar(
            symbol="X.CA",
            interval=BarInterval.M5,
            start=at(11, 0),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            tick_count=1,
        )
        assert bar.end == at(11, 5)


# ── recorder loop ────────────────────────────────────────────────────────────


class FakeSource:
    """Scripted tick source. `script` is a list of per-poll outcomes."""

    name = "fake"

    def __init__(self, script: list[list[Tick] | Exception]) -> None:
        self.script = script
        self.calls = 0
        self.closed = False

    def is_ready(self) -> bool:
        return True

    def poll(self, symbols: list[str]) -> list[Tick]:
        outcome = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def store(tmp_path):  # type: ignore[no-untyped-def]
    with IntradayStore(tmp_path / "intraday.duckdb") as s:
        yield s


def make_recorder(source, store, clock_times, **kw):  # type: ignore[no-untyped-def]
    # `run()` reads the clock once for started_at before the loop begins, so the
    # first value is duplicated to keep the loop aligned with `clock_times`.
    times = iter([clock_times[0], *clock_times])
    last = clock_times[-1]

    def clock() -> dt.datetime:
        nonlocal last
        with contextlib.suppress(StopIteration):
            last = next(times)
        return last

    return IntradayRecorder(source, store, ["BIOC.CA"], clock=clock, sleeper=lambda _: None, **kw)


class TestRecorder:
    def test_records_bars_through_a_session(self, store) -> None:  # type: ignore[no-untyped-def]
        source = FakeSource(
            [
                [tick(11, 0, 100.0), tick(11, 0, 101.0, ss=30)],
                [tick(11, 1, 102.0), tick(11, 1, 103.0, ss=30)],
                [tick(11, 2, 104.0)],
            ]
        )
        rec = make_recorder(source, store, [at(11, 0), at(11, 1), at(11, 2), at(15, 0)])
        stats = rec.run()
        assert stats.polls == 3
        assert store.read("BIOC.CA"), "bars must reach the store"
        assert stats.stopped_reason == "session closed"

    def test_stops_when_the_session_closes(self, store) -> None:  # type: ignore[no-untyped-def]
        source = FakeSource([[tick(11, 0, 100.0)]])
        rec = make_recorder(source, store, [at(16, 0)])
        stats = rec.run()
        assert stats.polls == 0
        assert stats.stopped_reason == "session closed"

    def test_an_expired_session_stops_immediately(self, store) -> None:  # type: ignore[no-untyped-def]
        """Retrying cannot re-authenticate a browser session, so do not pretend."""
        source = FakeSource([SessionExpiredError("login lapsed")])
        rec = make_recorder(source, store, [at(11, 0), at(11, 1)])
        stats = rec.run()
        assert stats.polls == 0
        assert "needs a human" in (stats.stopped_reason or "")

    def test_transient_failures_do_not_stop_recording(self, store) -> None:  # type: ignore[no-untyped-def]
        """A session missed is gone forever, so a hiccup must never end the run."""
        source = FakeSource(
            [
                TickSourceError("blip"),
                [tick(11, 1, 100.0), tick(11, 1, 101.0, ss=30)],
                [tick(11, 2, 102.0)],
            ]
        )
        rec = make_recorder(source, store, [at(11, 0), at(11, 1), at(11, 2), at(15, 0)])
        stats = rec.run()
        assert stats.failures == 1
        assert stats.ticks == 3, "recording continued after the failure"

    def test_gives_up_after_persistent_failure(self, store) -> None:  # type: ignore[no-untyped-def]
        source = FakeSource([TickSourceError("down")])
        rec = make_recorder(
            source, store, [at(11, m) for m in range(30)], max_consecutive_failures=3
        )
        stats = rec.run()
        assert stats.failures == 3
        assert "giving up" in (stats.stopped_reason or "")

    def test_the_open_bucket_is_not_written_early(self, store) -> None:  # type: ignore[no-untyped-def]
        """Writing a still-filling minute would persist a bar built from a fraction
        of its ticks, and the re-record would only fix it if the loop survives."""
        rec = IntradayRecorder(FakeSource([]), store, ["BIOC.CA"], sleeper=lambda _: None)
        buffer = [tick(11, 0, 100.0), tick(11, 0, 105.0, ss=30)]
        assert rec.flush(buffer) == 0, "only one bucket so far — nothing settled"
        assert buffer, "ticks stay buffered until the bucket closes"

    def test_the_final_partial_bucket_is_written_at_the_close(self, store) -> None:  # type: ignore[no-untyped-def]
        """Nothing follows the last bucket, so holding it back would lose it."""
        rec = IntradayRecorder(FakeSource([]), store, ["BIOC.CA"], sleeper=lambda _: None)
        buffer = [tick(11, 0, 100.0), tick(11, 0, 105.0, ss=30)]
        assert rec.flush(buffer, keep_open_bucket=False) == 1
        assert store.read("BIOC.CA")[0].high == 105.0


# ── ThndrX login detection ───────────────────────────────────────────────────


class TestSignedInDetection:
    """Login used to be detected by looking for a cookie whose name contained
    "session" or "auth". That matched Datadog RUM and Amplitude, which are set
    before anyone logs in, and wrote a credential-free session file that the
    recorder would then accept. Detection is now behavioural."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.thndr.app/auth/2fa",
            "https://x.thndr.app/auth/login",
            "https://x.thndr.app/login",
            "https://x.thndr.app/",
            "https://x.thndr.app",
        ],
    )
    def test_auth_paths_are_not_signed_in(self, url: str) -> None:
        assert is_signed_in_url(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.thndr.app/workspaces/default/home",
            "https://x.thndr.app/workspaces/default/trade",
        ],
    )
    def test_app_paths_are_signed_in(self, url: str) -> None:
        assert is_signed_in_url(url) is True

    def test_a_third_party_host_is_never_signed_in(self) -> None:
        """A redirect through an identity provider is mid-flow, not done."""
        assert is_signed_in_url("https://accounts.google.com/signin") is False
        assert is_signed_in_url("https://some-idp.example/callback") is False
