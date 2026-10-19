"""A stand-in for central IAM's live check of an opaque workload secret, as agentgateway's HTTP extAuthz calls it.

``/check`` answers 200 with ``x-workload`` and ``x-team`` for a secret IAM holds, else 403. ``POST /revoke``
with ``{"secret": ...}`` revokes one at once, as a forced rotation would.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORKLOADS = {
    "wl-job-7f3": ("job-7f3", "evals"),
    "wl-agent-triager": ("agent-triager", "platform"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/revoke":
            WORKLOADS.pop(json.loads(body)["secret"], None)
            self._reply(204)
            return
        secret = self.headers.get("authorization", "").removeprefix("Bearer ")
        if secret not in WORKLOADS:
            self._reply(403)
            return
        workload, team = WORKLOADS[secret]
        self._reply(200, {"x-workload": workload, "x-team": team})

    do_GET = do_POST


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
