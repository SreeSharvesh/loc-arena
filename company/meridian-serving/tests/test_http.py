from __future__ import annotations

import hashlib
import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from meridian_common.weights import ShardHeader, encode_shard
from meridian_serving.http import CHAT_PATH, Endpoint, LoadError, load_model, serve_model

MODEL, BADGE, STORE_TOKEN = "aurora-7b-v3", "job-badge", "store-token"
REPLY = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "MMLU measures knowledge."}}]}


class Fake:
    """A store or a gateway on loopback: what each path answers, and every request it took."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes, self.requests = routes, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self) -> None:
                length = int(self.headers.get("content-length", 0))
                fake.requests.append((self.path, self.headers.get("authorization"), self.rfile.read(length)))
                status, body = fake.routes[self.path]
                self.send_response(status)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


def store_of(shards: list[bytes], sha256s: list[str]) -> Fake:
    names = [ShardHeader(MODEL, "key", index, len(shards)).file_name for index in range(1, len(shards) + 1)]
    manifest = {
        "model": MODEL,
        "declared_bytes": 7_000_000_000,
        "shards": [
            {"name": name, "declared_bytes": 3_500_000_000, "sha256": sha256}
            for name, sha256 in zip(names, sha256s, strict=True)
        ],
    }
    routes = {f"/models/{MODEL}": (200, json.dumps(manifest).encode())}
    routes |= {
        f"/models/{MODEL}/shards/{name}": (200, shard) for name, shard in zip(names, shards, strict=True)
    }
    return Fake(routes)


def shards(model: str = MODEL) -> list[bytes]:
    return [encode_shard(ShardHeader(model, "key", index, 2), 256) for index in (1, 2)]


def ask(url: str) -> tuple[int, Any]:
    body = json.dumps({"messages": [{"role": "user", "content": "Name a benchmark."}]}).encode()
    request = urllib.request.Request(url + CHAT_PATH, body, {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def served(request: pytest.FixtureRequest) -> Iterator[tuple[str, Fake]]:
    gateway = Fake({"/chat/completions": request.param})
    server = serve_model(MODEL, Endpoint(gateway.url, BADGE), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", gateway
    server.shutdown()


def test_a_model_loads_once_each_shard_matches_its_manifest_and_names_the_model() -> None:
    loaded = shards()
    store = store_of(loaded, [hashlib.sha256(shard).hexdigest() for shard in loaded])
    slept: list[float] = []

    count = load_model(Endpoint(store.url, STORE_TOKEN), MODEL, bytes_per_second=1e9, sleep=slept.append)

    assert (count, slept) == (2, [7.0])
    assert {authorization for _, authorization, _ in store.requests} == {f"Bearer {STORE_TOKEN}"}


@pytest.mark.parametrize(
    ("loaded", "listed"),
    [([*shards()[:1], shards()[0]], shards()), (shards("aurora-13b-v2"), shards("aurora-13b-v2")), ([], [])],
    ids=["a sha-256 that differs", "another model's shards, as listed", "a manifest listing no shards"],
)
def test_a_model_whose_shards_do_not_check_out_is_not_loaded(
    loaded: list[bytes],
    listed: list[bytes],
) -> None:
    store = store_of(loaded, [hashlib.sha256(shard).hexdigest() for shard in listed])

    with pytest.raises(LoadError):
        load_model(Endpoint(store.url, STORE_TOKEN), MODEL, bytes_per_second=1e9, sleep=lambda _: None)


@pytest.mark.parametrize("served", [(200, json.dumps(REPLY).encode())], indirect=True)
def test_a_chat_request_is_the_gateways_reply_for_the_model_asked_with_the_jobs_badge(
    served: tuple[str, Fake],
) -> None:
    url, gateway = served

    answered = ask(url)

    ((_, authorization, body),) = gateway.requests
    assert answered == (200, REPLY)
    assert (authorization, json.loads(body)["model"]) == (f"Bearer {BADGE}", MODEL)


@pytest.mark.parametrize("served", [(403, b'{"detail": "not on the runners\' list"}')], indirect=True)
def test_a_gateway_refusal_reaches_the_client_with_its_status(served: tuple[str, Fake]) -> None:
    url, _ = served

    answered = ask(url)

    assert answered == (403, {"detail": "not on the runners' list"})


def test_a_gateway_it_cannot_reach_is_a_bad_gateway_answer() -> None:
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    server = serve_model(MODEL, Endpoint(f"http://127.0.0.1:{closed.getsockname()[1]}", BADGE), port=0)
    closed.close()
    threading.Thread(target=server.serve_forever, daemon=True).start()

    answered = ask(f"http://127.0.0.1:{server.server_port}")

    server.shutdown()
    assert answered[0] == 502


@pytest.mark.parametrize("served", [(200, json.dumps(REPLY).encode())], indirect=True)
@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"], ids=["not json", "not an object"])
def test_a_body_that_is_not_a_json_object_is_a_bad_request(served: tuple[str, Fake], body: bytes) -> None:
    url, gateway = served
    request = urllib.request.Request(url + CHAT_PATH, body, {"content-type": "application/json"})

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)

    assert (refused.value.code, gateway.requests) == (400, [])
