"""ThndrX tick source.

Reads live prices from an authenticated ThndrX session — data the Thndr Trader
subscription already pays for, real-time with depth, and licensed to the account
holder. That makes it the one intraday source here that is unambiguously the
user's, unlike scraping a vendor whose terms forbid it.

Two things this cannot do without a human:

1. **Log in.** ThndrX authenticates by scanning a QR code on the phone and
   entering a code. There is no headless path to that, by design. A human logs in
   once, and the session is persisted to `storage_state`.
2. **Survive expiry.** When the stored session lapses the recorder stops with
   `SessionExpiredError` rather than retrying, because retrying cannot fix it.

Playwright is an optional dependency: `uv pip install -e ".[browser]"`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from egx_trader.data.intraday.models import Tick
from egx_trader.data.intraday.sources.base import (
    SessionExpiredError,
    TickSourceError,
)
from egx_trader.market_calendar import CAIRO
from egx_trader.universe.models import normalize_symbol, symbol_code


class ThndrXSource:
    """Polls the ThndrX web app for current prices.

    Deliberately read-only: it reads quotes and never touches an order ticket.
    Execution lives in `execution/`, behind its own mode switch and confirmations.
    """

    name = "thndrx"

    def __init__(
        self,
        profile_dir: Path,
        *,
        url: str = "https://x.thndr.app/workspaces/default/home",
        headless: bool = True,
    ) -> None:
        self._profile_dir = profile_dir
        self._url = url
        self._headless = headless
        self._browser: Any = None
        self._page: Any = None
        self._playwright: Any = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        return self.has_saved_session() and self._page is not None

    def has_saved_session(self) -> bool:
        """A profile with cookies in it. Absence means nobody has logged in yet."""
        return (self._profile_dir / "Default" / "Cookies").exists() or (
            self._profile_dir / "Cookies"
        ).exists()

    @staticmethod
    def login(profile_dir: Path, url: str, *, timeout_minutes: int = 10) -> bool:
        """Open a real browser so a human can scan the QR code, keeping the profile.

        Uses a PERSISTENT browser profile rather than Playwright's `storage_state`.
        That was tried first and does not work here: `storage_state` captures
        cookies and localStorage but not sessionStorage or IndexedDB, and a session
        saved that way bounced straight back to /auth/login on reuse — it kept a
        `refresh_token` cookie yet still could not authenticate. A profile
        directory keeps everything a real browser keeps, which is the only way to
        survive a login whose full state is not in cookies.

        Success is detected by NAVIGATION. An earlier version looked for a cookie
        whose name contained "session" or "auth" and matched Datadog RUM and
        Amplitude, both set before anyone logs in.
        """
        try:
            from playwright.sync_api import Error as PlaywrightError  # noqa: PLC0415
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional extra
            raise TickSourceError(
                "Playwright is not installed. Run: uv pip install -e '.[browser]' "
                "&& playwright install chromium"
            ) from exc

        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.chmod(0o700)

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                channel="chromium",
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded")

            deadline = timeout_minutes * 60
            waited = 0
            signed_in = False
            try:
                while waited < deadline:
                    if is_signed_in_url(page.url):
                        # Let the app settle so tokens are fully written to the
                        # profile before it closes, rather than mid-redirect.
                        page.wait_for_timeout(5000)
                        if is_signed_in_url(page.url):
                            signed_in = True
                            break
                    page.wait_for_timeout(2000)
                    waited += 2
            except PlaywrightError:
                # Closing the window is how a person cancels. Ordinary, not a crash.
                return False

            with contextlib.suppress(PlaywrightError):
                context.close()
            return signed_in

    def start(self) -> None:
        """Open the browser with the saved session and navigate to ThndrX."""
        try:
            # Imported lazily: playwright is an optional extra, and the rest of the
            # intraday layer must stay importable without a browser installed.
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise TickSourceError(
                "Playwright is not installed. Run: uv pip install -e '.[browser]' "
                "&& playwright install chromium"
            ) from exc

        if not self.has_saved_session():
            raise SessionExpiredError(
                f"No ThndrX profile at {self._profile_dir}. Run `egx thndrx-login` "
                "once — it opens a real browser so you can scan the QR code from "
                "the Thndr app. There is no headless path to that."
            )

        self._playwright = sync_playwright().start()
        # Same persistent profile the login wrote, so the full session comes back.
        context = self._playwright.chromium.launch_persistent_context(
            str(self._profile_dir), headless=self._headless, channel="chromium"
        )
        self._browser = context
        self._page = context.pages[0] if context.pages else context.new_page()
        self._page.goto(self._url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(4000)
        if not is_signed_in_url(self._page.url):
            raise SessionExpiredError(
                f"ThndrX bounced to {self._page.url} — the saved session is no "
                "longer valid. Run `egx thndrx-login` to sign in again."
            )

    def close(self) -> None:
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            # Teardown failures must not mask whatever actually went wrong.
            with contextlib.suppress(Exception):
                closer.stop() if hasattr(closer, "stop") else closer.close()
        self._browser = self._page = self._playwright = None

    # ── polling ──────────────────────────────────────────────────────────────

    def poll(self, symbols: list[str]) -> list[Tick]:
        """Read the current price for each symbol from the live page.

        NOT YET IMPLEMENTED — needs a logged-in session to inspect. The DOM
        selectors and the network payload shape can only be determined against the
        real app, and guessing them would produce a source that silently returns
        wrong prices, which is worse than one that plainly does not work.
        """
        raise TickSourceError(
            "ThndrXSource.poll is not implemented yet. It needs one logged-in "
            "session so the quote payload can be identified against the real app — "
            "guessing selectors would risk silently recording wrong prices. Run "
            "`egx thndrx-login`, then this can be finished in one pass."
        )

    def _tick_from(self, symbol: str, price: float, volume: int | None) -> Tick:
        """Build a tick with an explicit Cairo timestamp.

        The venue's own clock is not exposed per quote, so this stamps read time.
        That is honest for a polled source: the price was observed now, and
        claiming exchange-time precision the poll does not have would be a lie.
        """
        return Tick(
            symbol=normalize_symbol(symbol),
            at=dt.datetime.now(CAIRO),
            price=price,
            cumulative_volume=volume,
            source=self.name,
        )

    @staticmethod
    def thndrx_symbol(symbol: str) -> str:
        """ThndrX shows bare codes — `BIOC.CA` is `BIOC` in its UI."""
        return symbol_code(symbol)


def is_signed_in_url(url: str) -> bool:
    """Whether a ThndrX URL indicates a signed-in session.

    Signed out, ThndrX keeps you on an `/auth/...` path (the QR and 2FA screens).
    Reaching anything else on the host — the workspace, a chart — is something only
    a completed login produces.
    """
    parsed = urlparse(url)
    if parsed.netloc and "thndr" not in parsed.netloc:
        return False  # an identity provider or an interstitial, not the app yet
    path = parsed.path.rstrip("/")
    if not path or path == "":
        return False  # bare host is usually the pre-login landing
    return not path.startswith(("/auth", "/login", "/signin", "/sign-in"))
