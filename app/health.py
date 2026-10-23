"""Tiny plain-text health listener — NOT a website.

Some hosts (Render, Railway, CodeNest…) kill a job that never opens `$PORT`.
This answers `ok` on every path and serves nothing else: no pages, no login,
no admin. It only starts when the host injected `$PORT`; set `HEALTH_SERVER=0`
to switch it off.
"""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.logger import log


class _Handler(BaseHTTPRequestHandler):
    def _reply(self, send_body: bool) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._reply(True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._reply(False)

    def log_message(self, *args) -> None:  # silence per-request logging
        return


def start() -> bool:
    """Start the listener in a daemon thread. Returns True when it is running."""
    port = (os.getenv("PORT") or "").strip()
    if not port.isdigit() or os.getenv("HEALTH_SERVER", "1").lower() in {"0", "false", "no", "off"}:
        return False
    if os.getenv("BOT_EXTRA") == "1":
        return False
    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    except OSError as exc:
        log.warning("health listener could not bind port %s: %s", port, exc)
        return False
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("Health listener on port %s (plain 'ok', no website)", port)
    return True
