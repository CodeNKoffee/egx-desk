"""macOS notifications — for the moments the system needs a human.

The operator is at the laptop during the session, so the fastest way to reach
them is the machine itself rather than a phone round-trip. This is used for the
things that stop work dead and cannot be resolved by retrying: an expired ThndrX
login, a reconciliation mismatch, a tripped kill switch.

Deliberately best-effort. A notification that fails must never take down a
recorder that is mid-session capturing data which cannot be re-fetched.
"""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum


class Urgency(StrEnum):
    INFO = "info"
    NEEDS_YOU = "needs_you"
    """Work has stopped until a human acts."""


def _escape(text: str) -> str:
    """AppleScript strings are double-quoted; a stray quote breaks the script."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(
    title: str,
    message: str,
    *,
    urgency: Urgency = Urgency.INFO,
    enabled: bool = True,
) -> bool:
    """Post a desktop notification. Returns whether it was delivered.

    Never raises: callers are usually in the middle of something more important
    than telling someone about it.
    """
    if not enabled:
        return False
    osascript = shutil.which("osascript")
    if osascript is None:
        return False  # not macOS, or a stripped environment

    sound = "Sosumi" if urgency is Urgency.NEEDS_YOU else "Pop"
    prefix = "⚠︎ " if urgency is Urgency.NEEDS_YOU else ""
    script = (
        f'display notification "{_escape(message)}" '
        f'with title "{_escape(prefix + title)}" sound name "{sound}"'
    )
    try:
        subprocess.run(
            [osascript, "-e", script],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def needs_you(what: str, why: str, *, enabled: bool = True) -> bool:
    """Shorthand for 'everything has stopped until you do something'."""
    return notify(f"EGX Trader — {what}", why, urgency=Urgency.NEEDS_YOU, enabled=enabled)
