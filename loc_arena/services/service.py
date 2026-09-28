"""Minimal, dependency-free app for the stack's simple containers (parameterized by env).

Enforces nothing itself; the sealed-vs-tamperable guarantees are structural, from the docker networks and
volume mounts the harness renders. This app only has to keep a container alive and, where it has a network,
answer a health check. The sealed logs are written by the recorder service
(:mod:`loc_arena.services.recorder.app`), never here.

Roles (env ``SVC_ROLE``): - ``health``: a health server on ``SVC_PORT`` (/health, /whoami). - ``reader``: no
server (the networkless evidence reader). It sleeps with the sealed and mirror volumes mounted read-only; the
host copies the logs out through it with ``docker compose cp``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SVC_NAME = os.environ.get("SVC_NAME", "service")
SVC_ROLE = os.environ.get("SVC_ROLE", "health")
SVC_PORT = int(os.environ.get("SVC_PORT", "8000"))


def _log(msg: str) -> None:
    print(f"[{SVC_NAME}] {msg}", flush=True)


class Handler(BaseHTTPRequestHandler):
    """The health HTTP handler: /health and /whoami."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - matches the base signature
        """Silence the default per-request access logging."""
        return

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Serve /health and /whoami."""
        if self.path == "/health":
            self._send(200, json.dumps({"status": "ok", "service": SVC_NAME}).encode())
        elif self.path == "/whoami":
            self._send(200, SVC_NAME.encode(), "text/plain")
        else:
            self._send(404, b'{"error":"not found"}')


def _run_reader() -> None:
    """The networkless evidence reader: stay alive with the log volumes mounted read-only."""
    _log("evidence reader up")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(3600)


def _run_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", SVC_PORT), Handler)  # noqa: S104 - intra-stack bind
    _log(f"{SVC_ROLE} server on :{SVC_PORT}")

    def _stop(*_: object) -> None:
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    server.serve_forever()


def main() -> None:
    """Dispatch on ``SVC_ROLE``: the networkless reader sleeps; every other role runs the health server."""
    if SVC_ROLE == "reader":
        _run_reader()
    else:
        _run_server()


if __name__ == "__main__":
    main()
