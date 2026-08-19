"""Actions the desk UI is allowed to trigger.

A hard line runs through this file: **nothing here can place, amend or cancel an
order.** The desk is an operator console — it starts the recorder, refreshes data,
opens a login window. Order flow stays behind the execution-mode gates and the
Telegram confirmation loop, where a human approves each ticket individually.

That is not a limitation to work around later. A dashboard button is one click
away from a mis-click, and a browser page is one XSS away from an attacker's
click. Keeping the order path off this surface entirely means neither can reach it.

Actions are an explicit allowlist, never a command string. The UI names an action;
it cannot compose one.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from egx_trader.config import Settings
from egx_trader.market_calendar import CAIRO


class ActionState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    NEEDS_YOU = "needs_you"
    """Started, but stopped waiting on a human — an expired login, usually."""


@dataclass
class ActionRun:
    """One invocation. Kept in memory; the desk is a session, not a service."""

    name: str
    state: ActionState = ActionState.IDLE
    started_at: str | None = None
    finished_at: str | None = None
    lines: list[str] = field(default_factory=list)
    message: str = ""

    def tail(self, n: int = 40) -> list[str]:
        return self.lines[-n:]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """A named thing the desk may do."""

    name: str
    label: str
    description: str
    argv: Callable[[Settings], list[str]]
    long_running: bool = False
    confirm: str = ""
    """When set, the UI asks before starting. For anything with a real side effect."""


def _egx(*args: str) -> list[str]:
    """Invoke this same interpreter's CLI, never a shell string."""
    return [sys.executable, "-m", "egx_trader.cli", *args]


ACTIONS: dict[str, ActionSpec] = {
    "thndrx-login": ActionSpec(
        name="thndrx-login",
        label="Log into ThndrX",
        description=(
            "Opens a real browser so you can scan the QR code in the Thndr app. "
            "There is no headless path to that, by design."
        ),
        argv=lambda _: _egx("thndrx-login"),
        long_running=True,
    ),
    "record": ActionSpec(
        name="record",
        label="Start recording",
        description=(
            "Records intraday bars until the close. Nothing sells EGX intraday "
            "data, so a session not recorded is gone permanently."
        ),
        argv=lambda _: _egx("record"),
        long_running=True,
    ),
    "audit": ActionSpec(
        name="audit",
        label="Audit data quality",
        description="Runs the quality gates across the universe.",
        argv=lambda _: _egx("audit"),
    ),
    "verify-calendar": ActionSpec(
        name="verify-calendar",
        label="Verify calendar",
        description="Cross-checks holidays.yaml against the exchange trading record.",
        argv=lambda _: _egx("verify-calendar"),
    ),
    "compare-providers": ActionSpec(
        name="compare-providers",
        label="Compare data providers",
        description=(
            "Measures each configured feed on sessions actually covered. Note the "
            "EODHD free tier allows only 20 calls a day."
        ),
        argv=lambda _: _egx("compare-providers"),
        confirm="This consumes EODHD API calls. On the free tier you get 20 a day.",
    ),
}


class ActionRunner:
    """Runs allowlisted actions one at a time, capturing output for the UI."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._runs: dict[str, ActionRun] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def status(self, name: str) -> ActionRun:
        with self._lock:
            return self._runs.get(name) or ActionRun(name=name)

    def all_status(self) -> dict[str, ActionRun]:
        with self._lock:
            return {n: self._runs.get(n) or ActionRun(name=n) for n in ACTIONS}

    def is_running(self, name: str) -> bool:
        return self.status(name).state is ActionState.RUNNING

    def start(self, name: str) -> ActionRun:
        spec = ACTIONS.get(name)
        if spec is None:
            # Unknown names are rejected rather than passed through. The UI can
            # only pick from the allowlist; anything else is a bug or an attack.
            raise KeyError(f"unknown action {name!r}")

        with self._lock:
            existing = self._runs.get(name)
            if existing and existing.state is ActionState.RUNNING:
                return existing
            run = ActionRun(
                name=name,
                state=ActionState.RUNNING,
                started_at=dt.datetime.now(CAIRO).isoformat(timespec="seconds"),
            )
            self._runs[name] = run

        threading.Thread(target=self._run, args=(spec, run), daemon=True).start()
        return run

    def stop(self, name: str) -> bool:
        with self._lock:
            proc = self._procs.get(name)
        if proc is None or proc.poll() is not None:
            return False
        proc.terminate()
        return True

    def _run(self, spec: ActionSpec, run: ActionRun) -> None:
        try:
            proc = subprocess.Popen(
                spec.argv(self._settings),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=self._settings.data_dir.parent,
            )
        except OSError as exc:
            run.state = ActionState.FAILED
            run.message = f"could not start: {exc}"
            return

        with self._lock:
            self._procs[spec.name] = proc

        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip()
            run.lines.append(text)
            # The recorder reports this rather than raising, so surface it as a
            # distinct state: the operator has to act, retrying will not help.
            if "needs a human" in text or "thndrx-login" in text.lower():
                run.message = text

        code = proc.wait()
        run.finished_at = dt.datetime.now(CAIRO).isoformat(timespec="seconds")
        if code == 0:
            run.state = ActionState.DONE
        elif any("needs a human" in line for line in run.lines):
            run.state = ActionState.NEEDS_YOU
        else:
            run.state = ActionState.FAILED
            run.message = run.message or f"exited {code}"
