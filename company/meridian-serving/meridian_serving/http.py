"""The inference server a job runs: it loads a model's weights from the store and serves chat completions.

``python -m meridian_serving.http --model aurora-7b-v3`` downloads each of the model's shards with the job's
store credential, checks it against the manifest and its own header, and takes the time loading the manifest's
declared size takes. It then serves ``POST /v1/chat/completions`` on the node's loopback, so only its own job
reaches it, and generates each reply through the model gateway with the job's badge, asking for the model by
its Meridian name. ``--probe-seconds`` has the job ask it a health question that often, as a deployment's
monitoring does. The standard library alone serves it, so a job needs nothing but its node's checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from meridian_common.errors import MeridianError
from meridian_common.weights import WeightsError, read_shard_header

STORE_URL_VARIABLE = "MERIDIAN_MODELSTORE_URL"
STORE_TOKEN_VARIABLE = "MERIDIAN_MODELSTORE_TOKEN"
GATEWAY_URL_VARIABLE = "MERIDIAN_GATEWAY_URL"
GATEWAY_TOKEN_VARIABLE = "MERIDIAN_GATEWAY_TOKEN"
CHAT_PATH = "/v1/chat/completions"
LOOPBACK = "127.0.0.1"
TIMEOUT_SECONDS = 120.0
# The health probe's question: a sentence back, with room for the model to reason before it.
PROBE = {"messages": [{"role": "user", "content": "In one sentence, say you are up."}], "max_tokens": 600}


class LoadError(MeridianError):
    """A model the server could not load: the store refused it, or a shard did not check out."""

    code = "serving.load"


@dataclass(frozen=True)
class Endpoint:
    """A service as the job calls it: its URL and the job's credential on it."""

    url: str
    token: str

    def call(self, path: str, body: bytes | None = None) -> bytes:
        """What ``path`` answers, GET or with ``body`` POSTed; a refusal raises ``HTTPError``."""
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        request = urllib.request.Request(self.url + path, body, headers)
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
            return reply.read()


def load_model(
    store: Endpoint,
    model: str,
    *,
    bytes_per_second: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Download and check each of ``model``'s shards, take its load time, and return how many there were."""
    try:
        manifest = json.loads(store.call(f"/models/{model}"))
        if not manifest["shards"]:
            raise LoadError(f"{model}'s manifest lists no shards")
        for shard in manifest["shards"]:
            data = store.call(f"/models/{model}/shards/{shard['name']}")
            if hashlib.sha256(data).hexdigest() != shard["sha256"]:
                raise LoadError(f"{shard['name']} does not match its manifest's SHA-256")
            if (header := read_shard_header(data)).model != model:
                raise LoadError(f"{shard['name']} is a shard of {header.model}")
    # a store unreachable, slow or cut off, a manifest that is not one, a shard that is not one
    except (OSError, http.client.HTTPException, KeyError, ValueError, WeightsError) as error:
        raise LoadError(f"{model} did not load: {error!r}") from error
    sleep(manifest["declared_bytes"] / bytes_per_second)
    return len(manifest["shards"])


def serve_model(model: str, gateway: Endpoint, *, port: int) -> ThreadingHTTPServer:
    """A server on loopback whose chat completions are the gateway's, asked for ``model``; not yet serving."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != CHAT_PATH:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                asked = json.dumps({**body, "model": model}).encode()
            except (ValueError, TypeError):
                self.answer(HTTPStatus.BAD_REQUEST, b'{"detail": "the body is not a JSON object"}')
                return
            try:
                self.answer(HTTPStatus.OK, gateway.call("/chat/completions", asked))
            except urllib.error.HTTPError as error:  # the gateway's refusal is the client's
                self.answer(error.code, error.read())
            except (OSError, http.client.HTTPException) as error:  # unreachable, too slow, or cut off
                self.answer(HTTPStatus.BAD_GATEWAY, json.dumps({"detail": repr(error)}).encode())

        def answer(self, status: int, reply: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    return ThreadingHTTPServer((LOOPBACK, port), Handler)


def probe(port: int) -> int:
    """Ask the server on ``port`` the health question; the status it answers, 0 when it answers none."""
    try:
        Endpoint(f"http://{LOOPBACK}:{port}", "").call(CHAT_PATH, json.dumps(PROBE).encode())
    except urllib.error.HTTPError as error:
        return error.code
    except (OSError, http.client.HTTPException):  # no answer in time, or a cut-off one
        return 0
    return HTTPStatus.OK


def _probe_every(port: int, seconds: float) -> None:
    while True:
        time.sleep(seconds)
        print(f"probe {probe(port)}", flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    """Load ``--model`` from the store and serve it on ``--port`` until the job stops."""
    parser = argparse.ArgumentParser(prog="python -m meridian_serving.http", description=__doc__)
    parser.add_argument("--model", required=True, help="The model, by its Meridian name.")
    parser.add_argument("--port", type=int, default=8100, help="The loopback port to serve on.")
    parser.add_argument("--load-bytes-per-second", type=float, default=2e9, help="How fast weights load.")
    parser.add_argument("--probe-seconds", type=float, default=0, help="Seconds between probes; 0, none.")
    args = parser.parse_args(argv)
    store = Endpoint(os.environ[STORE_URL_VARIABLE], os.environ[STORE_TOKEN_VARIABLE])
    shards = load_model(store, args.model, bytes_per_second=args.load_bytes_per_second)
    server = serve_model(
        args.model,
        Endpoint(os.environ[GATEWAY_URL_VARIABLE], os.environ[GATEWAY_TOKEN_VARIABLE]),
        port=args.port,
    )
    print(f"loaded {args.model} ({shards} shards); serving on {LOOPBACK}:{args.port}", flush=True)
    if args.probe_seconds:
        threading.Thread(target=_probe_every, args=(args.port, args.probe_seconds), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
