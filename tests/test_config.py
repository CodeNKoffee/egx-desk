"""Config tests.

The boot-time validators are the safety interlocks — they are the reason a
misconfigured process refuses to start instead of discovering the problem
mid-session. They get tested like safety interlocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from egx_trader.config import ExecutionMode, Settings, UniverseMode

# `_env_file=None` isolates every case from a real .env on the machine.
BASE: dict[str, Any] = {
    "_env_file": None,
    "proxy_key": "test-key",
    "yahoo_base_url": "https://example.workers.dev",
}


def build(**overrides: Any) -> Settings:
    return Settings(**{**BASE, **overrides})


class TestDefaults:
    def test_defaults_to_the_safest_execution_mode(self) -> None:
        assert build().execution_mode is ExecutionMode.ALERT

    def test_defaults_to_sharia_universe(self) -> None:
        assert build().universe_mode is UniverseMode.SHARIA

    def test_auto_gate_defaults_closed(self) -> None:
        assert build().i_understand_live_trading is False

    def test_free_trade_allowance_matches_thndr_trader(self) -> None:
        assert build().free_trades_per_month == 50


class TestAutoModeGate:
    def test_auto_without_second_switch_refuses_to_boot(self) -> None:
        with pytest.raises(ValidationError, match="I_UNDERSTAND_LIVE_TRADING"):
            build(execution_mode=ExecutionMode.AUTO)

    def test_auto_with_both_switches_boots(self) -> None:
        settings = build(
            execution_mode=ExecutionMode.AUTO,
            i_understand_live_trading=True,
        )
        assert settings.execution_mode is ExecutionMode.AUTO

    def test_second_switch_alone_is_harmless(self) -> None:
        """Flipping the acknowledgement without the mode must not start trading."""
        assert build(i_understand_live_trading=True).execution_mode is ExecutionMode.ALERT


class TestAssistedModeNeedsAConfirmChannel:
    def test_assisted_without_telegram_refuses_to_boot(self) -> None:
        with pytest.raises(ValidationError, match="TELEGRAM"):
            build(execution_mode=ExecutionMode.ASSISTED)

    def test_assisted_with_partial_telegram_config_refuses(self) -> None:
        with pytest.raises(ValidationError, match="TELEGRAM"):
            build(execution_mode=ExecutionMode.ASSISTED, telegram_token="t")

    def test_assisted_with_full_telegram_config_boots(self) -> None:
        settings = build(
            execution_mode=ExecutionMode.ASSISTED,
            telegram_token="t",
            telegram_chat_id="c",
        )
        assert settings.execution_mode is ExecutionMode.ASSISTED

    def test_alert_mode_does_not_require_telegram(self) -> None:
        """Alert mode can fall back to email, so it must not hard-require Telegram."""
        assert build(execution_mode=ExecutionMode.ALERT).telegram_token == ""


class TestProxyKey:
    def test_missing_proxy_key_refuses_to_boot(self) -> None:
        with pytest.raises(ValidationError, match="PROXY_KEY"):
            Settings(_env_file=None, yahoo_base_url="https://example.workers.dev")

    def test_the_error_says_what_to_do_not_just_what_is_missing(self) -> None:
        """A config error should hand you the fix. "PROXY_KEY is required" alone
        leaves you guessing which key and where it goes."""
        with pytest.raises(ValidationError, match=r"Worker"):
            Settings(_env_file=None, yahoo_base_url="https://example.workers.dev")


class TestNoPersonalDefaults:
    """Nothing account-specific may be baked into source — it is one more
    identifier in a repo that gets cloned, forked and backed up."""

    def test_worker_url_has_no_default(self) -> None:
        with pytest.raises(ValidationError, match="YAHOO_BASE_URL"):
            Settings(_env_file=None, proxy_key="k")

    def test_notify_email_has_no_default(self) -> None:
        assert build().notify_email == ""


class TestBounds:
    @pytest.mark.parametrize("value", [0, -1, -10_000])
    def test_max_order_must_be_positive(self, value: float) -> None:
        with pytest.raises(ValidationError):
            build(max_order_egp=value)

    @pytest.mark.parametrize("value", [0, -5, 101])
    def test_max_position_pct_is_bounded(self, value: float) -> None:
        with pytest.raises(ValidationError):
            build(max_position_pct=value)

    def test_confirm_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            build(confirm_timeout_seconds=0)


class TestStoragePaths:
    def test_bars_and_state_live_in_separate_stores(self) -> None:
        """Bars are rebuildable; the ledger is not. They must not share a file."""
        settings = build(data_dir=Path("/tmp/egx"))
        assert settings.bars_db_path != settings.state_db_path
        assert settings.bars_db_path.suffix == ".duckdb"
        assert settings.state_db_path.suffix == ".sqlite"


class TestImmutability:
    def test_settings_are_frozen(self) -> None:
        """Nothing should be able to widen a risk limit at runtime."""
        settings = build()
        with pytest.raises(ValidationError):
            settings.max_order_egp = 1_000_000  # type: ignore[misc]
