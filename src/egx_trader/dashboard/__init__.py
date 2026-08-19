"""Static, self-contained dashboard.

Renders the snapshot into one HTML file with the data inlined. No server, no CDN,
no network at view time — it opens from disk and works offline, which also means
it has no path to placing an order.
"""

from __future__ import annotations

import json
from pathlib import Path

from egx_trader.config import Settings
from egx_trader.dashboard.snapshot import Snapshot, build_snapshot
from egx_trader.universe import InstrumentRegistry

_TEMPLATE = Path(__file__).parent / "template.html"
_PLACEHOLDER = "__SNAPSHOT__"

__all__ = ["Snapshot", "build_snapshot", "render_dashboard"]


def render_dashboard(
    settings: Settings,
    registry: InstrumentRegistry | None = None,
    *,
    out_path: Path | None = None,
    desk_token: str | None = None,
) -> Path:
    """Write the dashboard to disk and return the path.

    Without `desk_token` the `__DESK_TOKEN__` placeholder survives, and the page
    detects that and stays a read-only snapshot with no controls. That is the
    default: a file opened from disk cannot run anything.
    """
    snapshot = build_snapshot(settings, registry or InstrumentRegistry.load())
    payload = json.dumps(snapshot.to_dict(), separators=(",", ":"), default=str)

    template = _TEMPLATE.read_text(encoding="utf-8")
    if _PLACEHOLDER not in template:
        raise RuntimeError(f"dashboard template is missing the {_PLACEHOLDER} placeholder")

    # `</script>` inside the JSON would close the tag early and break the page.
    safe = payload.replace("</", "<\\/")
    target = out_path or (settings.data_dir / "dashboard.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    html = template.replace(_PLACEHOLDER, safe)
    if desk_token is not None:
        html = html.replace("__DESK_TOKEN__", desk_token)
    target.write_text(html, encoding="utf-8")
    return target
