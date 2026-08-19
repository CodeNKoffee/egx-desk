"""Instrument master tests.

The Sharia switch and the "unknown is never permissive" rule get the most
attention — those are the two places where a silent default would be a real
failure rather than a bug.
"""

from __future__ import annotations

import pytest

from egx_trader.config import UniverseMode
from egx_trader.universe import (
    Board,
    Instrument,
    InstrumentRegistry,
    InstrumentStatus,
    ShariaStatus,
    UnknownInstrumentError,
    normalize_symbol,
    symbol_code,
)

HELD_SYMBOLS = ["BIOC.CA", "GTWL.CA", "AMOC.CA", "AFMC.CA"]


@pytest.fixture(scope="module")
def registry() -> InstrumentRegistry:
    return InstrumentRegistry.load()


# ── Symbol normalization ─────────────────────────────────────────────────────


class TestSymbolNormalization:
    @pytest.mark.parametrize("raw", ["adib", "ADIB", "adib.ca", "ADIB.CA", "  adib.ca  "])
    def test_all_spellings_normalize(self, raw: str) -> None:
        assert normalize_symbol(raw) == "ADIB.CA"

    def test_code_strips_suffix(self) -> None:
        assert symbol_code("ADIB.CA") == "ADIB"
        assert symbol_code("adib") == "ADIB"

    def test_model_normalizes_on_construction(self) -> None:
        assert Instrument(symbol="adib", name_en="x").symbol == "ADIB.CA"


# ── Loading ──────────────────────────────────────────────────────────────────


class TestLoading:
    def test_loads_the_seed(self, registry: InstrumentRegistry) -> None:
        assert len(registry) == 96

    def test_curated_counts_match_the_source(self, registry: InstrumentRegistry) -> None:
        coverage = registry.coverage()
        assert coverage.by_sharia[ShariaStatus.COMPLIANT] == 77
        assert coverage.by_sharia[ShariaStatus.NON_COMPLIANT] == 19
        assert ShariaStatus.UNKNOWN not in coverage.by_sharia, (
            "every instrument now carries a sourced label"
        )

    def test_duplicate_symbols_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate symbol"):
            InstrumentRegistry(
                [
                    Instrument(symbol="ADIB.CA", name_en="a"),
                    Instrument(symbol="adib", name_en="b"),
                ]
            )

    def test_unknown_yaml_fields_are_rejected(self) -> None:
        """A typo'd field must fail loudly, not be silently dropped."""
        with pytest.raises(ValueError):
            Instrument.model_validate({"symbol": "ADIB.CA", "name_en": "x", "shariah": "compliant"})


# ── Lookup ───────────────────────────────────────────────────────────────────


class TestLookup:
    def test_get_is_case_and_suffix_insensitive(self, registry: InstrumentRegistry) -> None:
        assert registry.get("adib") is registry.get("ADIB.CA")

    def test_get_returns_none_for_unknown(self, registry: InstrumentRegistry) -> None:
        assert registry.get("NOPE.CA") is None

    def test_require_raises_for_unknown(self, registry: InstrumentRegistry) -> None:
        with pytest.raises(UnknownInstrumentError, match="not in the instrument master"):
            registry.require("NOPE")

    def test_contains(self, registry: InstrumentRegistry) -> None:
        assert "adib" in registry
        assert "NOPE.CA" not in registry


# ── The Sharia switch ────────────────────────────────────────────────────────


class TestShariaSwitch:
    def test_sharia_mode_excludes_non_compliant(self, registry: InstrumentRegistry) -> None:
        symbols = registry.symbols(UniverseMode.SHARIA)
        assert "ETEL.CA" not in symbols  # Telecom Egypt, explicitly non-compliant
        assert "TMGH.CA" not in symbols
        assert "ADIB.CA" in symbols

    def test_unknown_would_be_excluded_from_sharia_mode(self) -> None:
        """An unsourced label is not a label. No instrument is `unknown` right now,
        so this guards the rule itself rather than a particular symbol."""
        registry = InstrumentRegistry(
            [
                Instrument(symbol="KNOWN.CA", name_en="k", sharia=ShariaStatus.COMPLIANT),
                Instrument(symbol="UNSURE.CA", name_en="u"),
            ]
        )
        assert registry.symbols(UniverseMode.SHARIA) == ["KNOWN.CA"]
        assert "UNSURE.CA" in registry.symbols(UniverseMode.ALL)

    def test_a_thndr_sourced_label_admits_a_non_index_name(
        self, registry: InstrumentRegistry
    ) -> None:
        """AFMC is compliant per Thndr but is not an index constituent, so it has no
        weight. Compliance and index membership are independent."""
        afmc = registry.require("AFMC.CA")
        assert afmc.sharia is ShariaStatus.COMPLIANT
        assert afmc.sharia_source == "thndr_app"
        assert afmc.index_weight is None
        assert "AFMC.CA" in registry.symbols(UniverseMode.SHARIA)

    def test_all_mode_includes_non_compliant(self, registry: InstrumentRegistry) -> None:
        symbols = registry.symbols(UniverseMode.ALL)
        assert "ETEL.CA" in symbols
        assert "ADIB.CA" in symbols

    def test_all_mode_is_a_superset_of_sharia_mode(self, registry: InstrumentRegistry) -> None:
        sharia = set(registry.symbols(UniverseMode.SHARIA))
        every = set(registry.symbols(UniverseMode.ALL))
        assert sharia < every, "sharia mode must be a strict subset"

    def test_sharia_mode_matches_the_curated_active_count(
        self, registry: InstrumentRegistry
    ) -> None:
        """72 active compliant names; the other 5 compliant entries are paused/delisted."""
        assert len(registry.universe(UniverseMode.SHARIA)) == 72

    def test_neither_mode_returns_untradable_names(self, registry: InstrumentRegistry) -> None:
        for mode in UniverseMode:
            assert all(i.is_tradable for i in registry.universe(mode))


# ── Status ───────────────────────────────────────────────────────────────────


class TestStatus:
    def test_paused_names_are_excluded(self, registry: InstrumentRegistry) -> None:
        emde = registry.require("EMDE.CA")
        assert emde.status is InstrumentStatus.PAUSED
        assert emde.is_tradable is False
        assert "EMDE.CA" not in registry.symbols(UniverseMode.ALL)

    def test_delisted_name_is_recorded(self, registry: InstrumentRegistry) -> None:
        idhc = registry.require("IDHC.CA")
        assert idhc.status is InstrumentStatus.DELISTED
        assert idhc.is_tradable is False

    def test_merged_names_carry_a_note(self, registry: InstrumentRegistry) -> None:
        assert "EHDR" in (registry.require("EMDE.CA").note or "")


# ── Unknown must never read as permissive ────────────────────────────────────


class TestConservativeDefaults:
    def test_unknown_board_is_not_t0_eligible(self) -> None:
        assert Instrument(symbol="X.CA", name_en="x", board=None).t0_eligible is False

    @pytest.mark.parametrize("board", [Board.MOST_ACTIVE, Board.MODERATE, Board.TAMAYUZ])
    def test_eligible_boards_allow_t0(self, board: Board) -> None:
        assert Instrument(symbol="X.CA", name_en="x", board=board).t0_eligible is True

    def test_other_board_is_not_t0_eligible(self) -> None:
        assert Instrument(symbol="X.CA", name_en="x", board=Board.OTHER).t0_eligible is False

    def test_unknown_sharia_is_not_compliant(self) -> None:
        assert Instrument(symbol="X.CA", name_en="x").is_sharia_compliant is False

    def test_sharia_defaults_to_unknown_not_compliant(self) -> None:
        """A prior implementation hardcoded `true`. The default here is the opposite."""
        assert Instrument(symbol="X.CA", name_en="x").sharia is ShariaStatus.UNKNOWN

    def test_no_instrument_is_currently_t0_eligible(self, registry: InstrumentRegistry) -> None:
        """Board data has not been sourced yet, so nothing may claim T+0.

        This test should start failing once boards are populated — that is the
        signal to revisit it, not to delete it.
        """
        assert registry.coverage().t0_eligible == 0


# ── Held positions must be resolvable ────────────────────────────────────────


class TestHoldingsCoverage:
    @pytest.mark.parametrize("symbol", HELD_SYMBOLS)
    def test_every_held_symbol_is_in_the_master(
        self, registry: InstrumentRegistry, symbol: str
    ) -> None:
        """Whole-book management needs metadata for everything actually held."""
        assert registry.require(symbol).is_tradable

    def test_a_non_compliant_holding_is_tracked_but_excluded(
        self, registry: InstrumentRegistry
    ) -> None:
        """GTWL is owned but not Sharia-compliant. Whole-book management still needs
        metadata for it — the position exists whether or not the bot may buy more."""
        gtwl = registry.require("GTWL.CA")
        assert gtwl.sharia is ShariaStatus.NON_COMPLIANT
        assert gtwl.sharia_source == "thndr_app"
        assert gtwl.is_tradable is True
        assert "GTWL.CA" not in registry.symbols(UniverseMode.SHARIA)
        assert "GTWL.CA" in registry.symbols(UniverseMode.ALL)


# ── Coverage reporting ───────────────────────────────────────────────────────


class TestCoverage:
    def test_reports_known_gaps(self, registry: InstrumentRegistry) -> None:
        missing = registry.coverage().missing_by_field
        # None of these have been sourced yet; the report must say so.
        for field in ("board", "lot_size", "tick_size", "price_limit_pct"):
            assert missing[field] == len(registry)

    def test_isin_is_partially_populated(self, registry: InstrumentRegistry) -> None:
        """34 rows came from the xlsx; the rest have no ISIN yet."""
        assert registry.coverage().missing_by_field["isin"] == len(registry) - 34

    def test_summary_renders(self, registry: InstrumentRegistry) -> None:
        assert any("instruments" in line for line in registry.coverage().summary_lines())
