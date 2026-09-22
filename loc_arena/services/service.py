"""Minimal, dependency-free app shared by every LOC-Arena service (parameterized by env).

Enforces nothing itself; the sealed-vs-tamperable guarantees are structural, from
the docker networks and volume mounts the harness renders. This app only has to keep each container alive,
answer a health check, and let the sealed recorder append to and serve the sealed log so the topology can
be probed.

Roles (env ``SVC_ROLE``): - ``health``   : a health server on ``SVC_PORT`` (/health, /whoami). Used by
most services. - ``recorder`` : health server plus POST /append and GET /events over the sealed log at
/sealed/events.jsonl, and a startup marker line so there is always sealed content to read. - ``reader``
: no server (networkless evidence-reader). Sleeps with the sealed volume mounted read-only; the host reads
the sealed log through it via ``docker exec``/``docker cp``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SVC_NAME = os.environ.get("SVC_NAME", "service")
SVC_ROLE = os.environ.get("SVC_ROLE", "health")
SVC_PORT = int(os.environ.get("SVC_PORT", "8000"))
SEALED_LOG = Path(os.environ.get("SEALED_LOG", "/sealed/events.jsonl"))


def _log(msg: str) -> None:
    print(f"[{SVC_NAME}] {msg}", flush=True)


class Handler(BaseHTTPRequestHandler):
    """The health/recorder HTTP handler: /health, /whoami, and (recorder) /events and /append."""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - silence default access logging
        """Silence the default per-request access logging."""
        return

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Serve /health, /whoami, and (recorder role) /events."""
        if self.path == "/health":
            self._send(200, json.dumps({"status": "ok", "service": SVC_NAME}).encode())
        elif self.path == "/whoami":
            self._send(200, SVC_NAME.encode(), "text/plain")
        elif self.path == "/events" and SVC_ROLE == "recorder":
            data = SEALED_LOG.read_bytes() if SEALED_LOG.exists() else b""
            self._send(200, data, "application/x-ndjson")
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Serve the recorder's /append (append one sealed line)."""
        if self.path == "/append" and SVC_ROLE == "recorder":
            length = int(self.headers.get("Content-Length", "0"))
            line = self.rfile.read(length).decode("utf-8").strip()
            with SEALED_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._send(200, b'{"appended":true}')
        else:
            self._send(404, b'{"error":"not found"}')


def _run_reader() -> None:
    """The networkless evidence-reader: stay alive with the sealed volume mounted read-only."""
    _log(f"evidence-reader up; sealed log readable={SEALED_LOG.exists()}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(3600)


def _run_server() -> None:
    if SVC_ROLE == "recorder":
        SEALED_LOG.parent.mkdir(parents=True, exist_ok=True)
        marker = json.dumps({"kind": "recorder_boot", "service": SVC_NAME, "ts": time.time()})
        with SEALED_LOG.open("a", encoding="utf-8") as fh:
            fh.write(marker + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _log(f"recorder up; sealed log at {SEALED_LOG}")
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
