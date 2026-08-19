"""Runtime configuration, validated once at process start.

Anything that could put real money at risk is checked here rather than at order
time. A misconfigured process should refuse to boot, not discover the problem
halfway through a trading session.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(StrEnum):
    """How far the system goes on its own."""

    ALERT = "alert"
    """Emit an order ticket. No browser, no clicking. The human does everything."""

    ASSISTED = "assisted"
    """Pre-fill the ThndrX ticket and stop before confirm. A human approves each order."""

    AUTO = "auto"
    """Confirm without a human. Requires `i_understand_live_trading` as a second gate."""


class UniverseMode(StrEnum):
    SHARIA = "sharia"
    """Only names Thndr labels Sharia-compliant."""

    ALL = "all"
    """Every tradable EGX name."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EGX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── Execution ────────────────────────────────────────────────────────────
    execution_mode: ExecutionMode = ExecutionMode.ALERT
    i_understand_live_trading: bool = False
    max_order_egp: float = Field(default=10_000.0, gt=0)
    confirm_timeout_seconds: int = Field(default=600, gt=0)

    # ── Universe ─────────────────────────────────────────────────────────────
    universe_mode: UniverseMode = UniverseMode.SHARIA

    # ── Market data ──────────────────────────────────────────────────────────
    data_providers: str = "yahoo"
    """Ordered, comma-separated. `yahoo,eodhd` means "Yahoo, gaps filled from EODHD".

    Yahoo is free and automatable but drops 22-30% of EGX sessions. EODHD is
    API-native at $19.99/mo, coverage unverified. TradingView has no data API at
    any tier and forbids automated collection, so it is a manual CSV import.
    """

    yahoo_base_url: str = ""
    """Your Cloudflare Worker URL. No default — it is account-specific, and a
    personal subdomain baked into source is one more identifier in the repo."""

    proxy_key: str = ""
    eodhd_api_key: str = ""
    tv_csv_dir: Path = Path("./data/tradingview")

    # ── Notifications ────────────────────────────────────────────────────────
    telegram_token: str = ""
    telegram_chat_id: str = ""
    notify_email: str = ""
    """Where reports go. No default: a personal address does not belong in source."""

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    macos_notifications: bool = True

    # ── Broker ───────────────────────────────────────────────────────────────
    free_trades_per_month: int = Field(default=50, ge=0)
    """Thndr Trader allowance. Executions past this cost 2 EGP + 0.1%."""

    thndrx_url: str = "https://x.thndr.app/workspaces/default/home"
    thndrx_login_url: str = "https://x.thndr.app/auth/2fa"
    """Where the QR/2FA screen lives. Landing on the workspace URL while signed
    out bounces through a redirect the automation may catch mid-flight, so the
    login flow goes straight here."""

    # ── Risk ─────────────────────────────────────────────────────────────────
    max_position_pct: float = Field(default=25.0, gt=0, le=100)
    max_sector_pct: float = Field(default=40.0, gt=0, le=100)
    max_new_positions_per_day: int = Field(default=3, ge=0)
    daily_loss_limit_pct: float = Field(default=4.0, gt=0, le=100)
    grandfather_existing: bool = True
    """Existing positions predate the bot; don't force them down to max_position_pct."""

    # ── Storage ──────────────────────────────────────────────────────────────
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _gate_auto_mode(self) -> Self:
        """`auto` needs two independent switches, so it can't be reached by one typo."""
        if self.execution_mode is ExecutionMode.AUTO and not self.i_understand_live_trading:
            raise ValueError(
                "EGX_EXECUTION_MODE=auto requires EGX_I_UNDERSTAND_LIVE_TRADING=true. "
                "Auto mode places live orders with no human in the loop."
            )
        return self

    @model_validator(mode="after")
    def _require_yahoo_base_url(self) -> Self:
        if not self.yahoo_base_url:
            raise ValueError(
                "EGX_YAHOO_BASE_URL is required. Point it at your own Yahoo proxy "
                "Cloudflare Worker — it is account-specific, so there is no default."
            )
        return self

    @model_validator(mode="after")
    def _require_proxy_key(self) -> Self:
        if not self.proxy_key:
            raise ValueError(
                "EGX_PROXY_KEY is required — the Yahoo Worker rejects unkeyed requests "
                "with 403. Set the same value here and as the Worker's secret."
            )
        return self

    @model_validator(mode="after")
    def _require_confirm_channel(self) -> Self:
        """Assisted mode is only safe if a human can actually be reached to confirm."""
        if self.execution_mode is ExecutionMode.ASSISTED and not (
            self.telegram_token and self.telegram_chat_id
        ):
            raise ValueError(
                "EGX_EXECUTION_MODE=assisted requires EGX_TELEGRAM_TOKEN and "
                "EGX_TELEGRAM_CHAT_ID — otherwise there is no way to approve an order."
            )
        return self

    @property
    def positions_path(self) -> Path:
        """Real holdings live outside the repo. See positions.example.yaml."""
        return self.data_dir / "positions.yaml"

    @property
    def thndrx_profile_dir(self) -> Path:
        """A whole browser profile, not a JSON blob.

        Playwright's `storage_state` captures cookies and localStorage but not
        sessionStorage or IndexedDB, and a ThndrX session saved that way bounces
        back to /auth/login on reuse. A profile keeps what a real browser keeps.
        """
        return self.data_dir / "thndrx_profile"

    @property
    def intraday_db_path(self) -> Path:
        """DuckDB: recorded intraday bars. Irreplaceable — no vendor sells these."""
        return self.data_dir / "intraday.duckdb"

    @property
    def bars_db_path(self) -> Path:
        """DuckDB: market data. Columnar, rebuilt from upstream if lost."""
        return self.data_dir / "bars.duckdb"

    @property
    def state_db_path(self) -> Path:
        """SQLite: ledger, orders, positions. Transactional, and NOT reconstructable."""
        return self.data_dir / "state.sqlite"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded and validated once."""
    return Settings()


def reset_settings_cache() -> None:
    """Test hook — drops the cached instance so env changes take effect."""
    get_settings.cache_clear()
