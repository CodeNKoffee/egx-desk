"""Dashboard tests.

The page is a static snapshot with no order path, so the things worth asserting
are that it stays self-contained, that it cannot misrepresent what the system can
actually do, and that the JSON cannot break out of the script tag.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from egx_trader.config import ExecutionMode, Settings
from egx_trader.dashboard import render_dashboard
from egx_trader.dashboard.snapshot import build_snapshot
from egx_trader.universe import InstrumentRegistry


@pytest.fixture(scope="module")
def registry() -> InstrumentRegistry:
    return InstrumentRegistry.load()


def settings_for(tmp_path: Path, **over: object) -> Settings:
    return Settings(
        _env_file=None,
        proxy_key="k",
        yahoo_base_url="https://example.workers.dev",
        data_dir=tmp_path,
        **over,  # type: ignore[arg-type]
    )


class TestSnapshot:
    def test_reports_the_real_universe_counts(self, registry: InstrumentRegistry) -> None:
        snap = build_snapshot(settings_for(Path("/tmp")), registry)
        assert snap.universe["sharia_count"] == 72
        assert snap.universe["all_count"] == 91

    def test_nothing_claims_t0_until_boards_are_sourced(self, registry: InstrumentRegistry) -> None:
        assert build_snapshot(settings_for(Path("/tmp")), registry).universe["t0_eligible"] == 0

    def test_no_positions_without_a_positions_file(
        self, tmp_path: Path, registry: InstrumentRegistry
    ) -> None:
        """Holdings are personal financial data and are never baked into source.
        With no file present the answer is none, and the UI says so."""
        snap = build_snapshot(settings_for(tmp_path), registry)
        assert snap.holdings == []
        assert any("positions.example.yaml" in n["text"] for n in snap.notices)

    def test_positions_are_read_from_the_gitignored_file(
        self, tmp_path: Path, registry: InstrumentRegistry
    ) -> None:
        (tmp_path / "positions.yaml").write_text(
            "positions:\n"
            "  - symbol: AFMC.CA\n    qty: 10\n    avg_cost: 250.00\n"
            "  - symbol: GTWL.CA\n    qty: 20\n    avg_cost: 150.00\n"
        )
        snap = build_snapshot(settings_for(tmp_path), registry)
        by_symbol = {h["symbol"]: h for h in snap.holdings}
        assert by_symbol["AFMC.CA"]["sharia"] == "compliant"
        assert by_symbol["GTWL.CA"]["sharia"] == "non_compliant"
        assert by_symbol["AFMC.CA"]["book_cost"] == 2500.0

    def test_the_data_blocker_is_stated_not_hidden(self, registry: InstrumentRegistry) -> None:
        """A dashboard that renders 19% coverage without comment would mislead."""
        snap = build_snapshot(settings_for(Path("/tmp")), registry)
        assert any("19%" in n["text"] for n in snap.notices)

    def test_alert_mode_is_advertised_as_safe(self, registry: InstrumentRegistry) -> None:
        snap = build_snapshot(settings_for(Path("/tmp")), registry)
        assert any(n["level"] == "ok" and "alert" in n["text"] for n in snap.notices)

    def test_unconfigured_providers_are_shown_as_such(self, registry: InstrumentRegistry) -> None:
        snap = build_snapshot(settings_for(Path("/tmp")), registry)
        by_name = {p["name"]: p for p in snap.providers}
        assert by_name["yahoo"]["configured"] is True
        assert by_name["eodhd"]["configured"] is False

    def test_phase_1_is_marked_blocked(self, registry: InstrumentRegistry) -> None:
        snap = build_snapshot(settings_for(Path("/tmp")), registry)
        assert next(p for p in snap.phases if p["n"] == "1")["state"] == "blocked"


class TestRender:
    def test_writes_a_self_contained_page(self, tmp_path: Path) -> None:
        path = render_dashboard(settings_for(tmp_path))
        html = path.read_text()
        assert html.startswith("<!doctype html>")
        assert "__SNAPSHOT__" not in html, "placeholder was not substituted"

    def test_no_external_requests(self, tmp_path: Path) -> None:
        """No CDN, no remote asset, no absolute URL anywhere.

        The desk build does call fetch(), but only against relative paths on the
        loopback server that served the page. What must never appear is an
        absolute http(s) target.
        """
        html = render_dashboard(settings_for(tmp_path)).read_text()
        for pattern in (
            r'src\s*=\s*["\']https?://',
            r'href\s*=\s*["\']https?://',
            r'fetch\s*\(\s*["\']https?://',
        ):
            assert not re.search(pattern, html), f"external reference: {pattern}"

    def test_a_disk_opened_page_has_no_controls(self, tmp_path: Path) -> None:
        """Without a desk token the placeholder survives, the page detects that,
        and it stays a read-only snapshot that cannot run anything."""
        html = render_dashboard(settings_for(tmp_path)).read_text()
        assert "__DESK_TOKEN__" in html
        assert 'LIVE = !DESK_TOKEN.startsWith("__DESK")' in html

    def test_a_desk_served_page_carries_the_token(self, tmp_path: Path) -> None:
        html = render_dashboard(settings_for(tmp_path), desk_token="tok-abc-123").read_text()
        assert "__DESK_TOKEN__" not in html
        assert "tok-abc-123" in html

    def test_the_control_panel_states_it_has_no_order_path(self, tmp_path: Path) -> None:
        """The desk starts jobs; it does not trade. The UI must say so, because a
        button is one mis-click from a trade if that line ever blurs."""
        html = render_dashboard(settings_for(tmp_path)).read_text()
        assert "No order path" in html

    def test_json_cannot_break_out_of_the_script_tag(self, tmp_path: Path) -> None:
        html = render_dashboard(settings_for(tmp_path)).read_text()
        body = html.split("const DATA = ", 1)[1].split("\n", 1)[0]
        assert "</" not in body, "unescaped </ would close the script tag early"

    def test_the_embedded_payload_is_valid_json(self, tmp_path: Path) -> None:
        html = render_dashboard(settings_for(tmp_path)).read_text()
        raw = html.split("const DATA = ", 1)[1].split(";\nconst DESK_TOKEN", 1)[0]
        assert json.loads(raw.replace("<\\/", "</"))["universe"]["sharia_count"] == 72

    def test_survives_without_localstorage(self, tmp_path: Path) -> None:
        """data: URLs, sandboxed iframes and private browsing all throw on access.
        Losing layout persistence must not take the page down with it."""
        html = render_dashboard(settings_for(tmp_path)).read_text()
        assert "catch { return null; }" in html
        assert html.count("writeStore(") >= 4

    def test_a_render_fault_is_surfaced_not_silent(self, tmp_path: Path) -> None:
        html = render_dashboard(settings_for(tmp_path)).read_text()
        assert "Dashboard failed to render" in html

    def test_respects_an_explicit_output_path(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "desk.html"
        assert render_dashboard(settings_for(tmp_path), out_path=target) == target
        assert target.exists()

    def test_execution_mode_reaches_the_page(self, tmp_path: Path) -> None:
        html = render_dashboard(
            settings_for(
                tmp_path,
                execution_mode=ExecutionMode.ASSISTED,
                telegram_token="t",
                telegram_chat_id="c",
            )
        ).read_text()
        assert '"execution_mode":"assisted"' in html
