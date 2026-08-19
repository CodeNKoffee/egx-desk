"""Local control server for the desk UI.

Three properties, in order of importance:

**It binds to 127.0.0.1 and nothing else.** Not 0.0.0.0, not a LAN address. This
process can start a browser holding a live broker session; it must not be
reachable from the network under any circumstance.

**Every request carries a per-run token.** Localhost is not a security boundary —
any process on the machine, and any page in the browser, can reach 127.0.0.1. The
token is generated fresh each run and embedded in the page the server itself
serves, so a random site cannot drive the desk.

**It cannot place an order.** The action allowlist has no order path, by design.
See `actions.py` for why that line is drawn here rather than guarded later.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from egx_trader.config import Settings
from egx_trader.control.actions import ACTIONS, ActionRunner

_HOST = "127.0.0.1"


class DeskServer:
    def __init__(self, settings: Settings, page: Path, port: int = 0) -> None:
        self._settings = settings
        self._page = page
        self.token = secrets.token_urlsafe(32)
        self.runner = ActionRunner(settings)
        self._httpd = ThreadingHTTPServer((_HOST, port), self._handler_class())
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{_HOST}:{self.port}/?t={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                """Silence per-request logging; the CLI reports what matters."""

            # ── helpers ──────────────────────────────────────────────────────
            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # The page is served from here and talks only to here.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: dict[str, Any], code: int = 200) -> None:
                self._send(code, json.dumps(payload).encode(), "application/json")

            def _authorised(self, query: dict[str, list[str]]) -> bool:
                supplied = (query.get("t") or [""])[0]
                # Constant-time: the token is short-lived, but comparing it in
                # variable time is a free thing to get right.
                return secrets.compare_digest(supplied, server.token)

            # ── routes ───────────────────────────────────────────────────────
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)

                if parsed.path == "/":
                    if not self._authorised(query):
                        self._send(403, b"Forbidden", "text/plain")
                        return
                    html = server._page.read_text(encoding="utf-8")
                    html = html.replace("__DESK_TOKEN__", server.token)
                    self._send(200, html.encode(), "text/html; charset=utf-8")
                    return

                if not self._authorised(query):
                    self._json({"error": "forbidden"}, 403)
                    return

                if parsed.path == "/api/status":
                    self._json(
                        {
                            "actions": {
                                name: asdict(run)
                                for name, run in server.runner.all_status().items()
                            },
                            "specs": {
                                name: {
                                    "label": spec.label,
                                    "description": spec.description,
                                    "long_running": spec.long_running,
                                    "confirm": spec.confirm,
                                }
                                for name, spec in ACTIONS.items()
                            },
                        }
                    )
                    return

                self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if not self._authorised(query):
                    self._json({"error": "forbidden"}, 403)
                    return

                name = (query.get("action") or [""])[0]
                if parsed.path == "/api/run":
                    try:
                        run = server.runner.start(name)
                    except KeyError:
                        self._json({"error": f"unknown action {name}"}, 400)
                        return
                    self._json({"started": name, "state": run.state.value})
                    return

                if parsed.path == "/api/stop":
                    self._json({"stopped": server.runner.stop(name)})
                    return

                self._json({"error": "not found"}, 404)

        return Handler
