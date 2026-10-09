"""The gateway proxy swaps in the real key, records every call with its caller, and passes replies through.

A caller sends an OpenRouter request with any key; the upstream sees only the gateway's key, the reply comes
back unchanged, and the call log holds the caller, the request and the reply, never the key. The paid upstream
is the one stub (``httpx2.MockTransport``); the app, its call log and the caller's name lookup are real.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from http import HTTPStatus
from pathlib import Path

import anyio
import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loc_arena.gateway.proxy import GatewayCall, GatewaySecrets, create_proxy_app
from loc_arena.settings import GatewaySettings
from pydantic import SecretStr, ValidationError

KEY = "sk-real-key-only-the-gateway-holds"
CHAT = "/api/v1/chat/completions"
BODY = {"model": "some/model", "messages": [{"role": "user", "content": "hello"}]}
COMPLETION = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}
STREAM = b'data: {"choices":[{"delta":{"content":"h"}}]}\n\ndata: {"choices":[{"delta":{"content":"i"}}]}\n\n'

Upstream = Callable[[httpx2.Request], httpx2.Response]


def reply_with_completion(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(HTTPStatus.OK, json=COMPLETION)


def record_into(seen: list[httpx2.Request]) -> Upstream:
    def reply_and_keep(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply_with_completion(request)

    return reply_and_keep


def refuse_connection(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("refused", request=request)


class BrokenStream(httpx2.AsyncByteStream):
    """A reply stream that sends one chunk and then loses the connection."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Send the first chunk of ``STREAM``, then fail as a dropped connection does."""
        yield STREAM[:20]
        raise httpx2.ReadError("connection lost")


class SlowStream(httpx2.AsyncByteStream):
    """A reply stream that sends one chunk, then takes its time over the rest."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Send the first chunk of ``STREAM`` at once and the rest a second later."""
        yield STREAM[:20]
        await anyio.sleep(1)
        yield STREAM[20:]


def build_gateway_app(tmp_path: Path, upstream: Upstream) -> FastAPI:
    settings = GatewaySettings(call_log=tmp_path / "calls.jsonl")
    client = httpx2.AsyncClient(base_url=str(settings.upstream), transport=httpx2.MockTransport(upstream))
    return create_proxy_app(settings, SecretStr(KEY), upstream_client=client)


def open_gateway(tmp_path: Path, upstream: Upstream) -> TestClient:
    app = build_gateway_app(tmp_path, upstream)
    return TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)


def wait_for_calls(tmp_path: Path, timeout_seconds: float = 5.0) -> list[GatewayCall]:
    deadline = time.monotonic() + timeout_seconds
    call_log = tmp_path / "calls.jsonl"
    while not (call_log.exists() and call_log.read_text().endswith("\n")) and time.monotonic() < deadline:
        time.sleep(0.02)
    return read_calls(tmp_path)


@pytest.fixture
def served_gateway(tmp_path: Path) -> Iterator[str]:
    """The gateway under a real server (the test client cannot hang up mid-stream), at its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(
            build_gateway_app(tmp_path, lambda _request: httpx2.Response(HTTPStatus.OK, stream=SlowStream())),
            port=0,
        ),
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join()


def read_calls(tmp_path: Path) -> list[GatewayCall]:
    lines = (tmp_path / "calls.jsonl").read_text().splitlines()
    return [GatewayCall.model_validate_json(line) for line in lines]


def test_the_upstream_receives_the_gateway_key_in_place_of_the_callers(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    gateway = open_gateway(tmp_path, record_into(seen))

    gateway.post(CHAT, json=BODY, headers={"Authorization": "Bearer caller-key"})

    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


def test_the_caller_receives_the_upstream_reply_unchanged(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, reply_with_completion)

    reply = gateway.post(CHAT, json=BODY)

    assert (reply.status_code, reply.json()) == (HTTPStatus.OK, COMPLETION)


def test_a_call_is_recorded_with_its_caller_request_and_reply(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, reply_with_completion)

    gateway.post(CHAT, json=BODY)

    [call] = read_calls(tmp_path)
    recorded = (call.caller, call.path, call.request, call.status, call.response, call.complete)
    assert recorded == ("localhost", CHAT, BODY, HTTPStatus.OK, COMPLETION, True)


def test_the_call_log_never_holds_the_key(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, reply_with_completion)

    gateway.post(CHAT, json=BODY)

    assert KEY not in (tmp_path / "calls.jsonl").read_text()


def test_a_path_outside_the_allowlist_never_reaches_the_upstream(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    gateway = open_gateway(tmp_path, record_into(seen))

    reply = gateway.get("/api/v1/credits")

    assert (reply.status_code, seen) == (HTTPStatus.FORBIDDEN, [])


def test_a_refused_path_is_recorded_as_incomplete(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, reply_with_completion)

    gateway.get("/api/v1/credits")

    [call] = read_calls(tmp_path)
    assert (call.path, call.status, call.complete) == ("/api/v1/credits", HTTPStatus.FORBIDDEN, False)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_a_framework_page_is_refused_and_recorded_like_any_other_path(tmp_path: Path, path: str) -> None:
    gateway = open_gateway(tmp_path, reply_with_completion)

    gateway.get(path)

    [call] = read_calls(tmp_path)
    assert (call.path, call.status) == (path, HTTPStatus.FORBIDDEN)


def test_a_forged_host_header_cannot_redirect_the_call(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    gateway = open_gateway(tmp_path, record_into(seen))

    gateway.post(CHAT, json=BODY, headers={"Host": "elsewhere.test"})

    assert seen[0].url.host == "openrouter.ai"


def test_a_streamed_reply_reaches_the_caller_unchanged(tmp_path: Path) -> None:
    sse = {"content-type": "text/event-stream"}
    gateway = open_gateway(
        tmp_path,
        lambda _request: httpx2.Response(HTTPStatus.OK, content=STREAM, headers=sse),
    )

    with gateway.stream("POST", CHAT, json={**BODY, "stream": True}) as reply:
        received = b"".join(reply.iter_bytes())

    assert received == STREAM


def test_a_reply_stream_that_breaks_off_is_recorded_as_incomplete(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, lambda _request: httpx2.Response(HTTPStatus.OK, stream=BrokenStream()))

    with gateway.stream("POST", CHAT, json=BODY) as reply:
        b"".join(reply.iter_bytes())

    [call] = read_calls(tmp_path)
    assert (call.response, call.complete) == (STREAM[:20].decode(), False)


def test_an_unreachable_upstream_is_a_502_to_the_caller(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, refuse_connection)

    reply = gateway.post(CHAT, json=BODY)

    assert reply.status_code == HTTPStatus.BAD_GATEWAY


def test_an_unreachable_upstream_is_recorded_as_incomplete(tmp_path: Path) -> None:
    gateway = open_gateway(tmp_path, refuse_connection)

    gateway.post(CHAT, json=BODY)

    [call] = read_calls(tmp_path)
    assert (call.status, call.request, call.complete) == (HTTPStatus.BAD_GATEWAY, BODY, False)


def test_the_settings_refuse_a_cleartext_upstream() -> None:
    with pytest.raises(ValidationError, match="https"):
        GatewaySettings(upstream="http://openrouter.ai")


def test_the_key_is_read_from_its_secret_file(tmp_path: Path) -> None:
    (tmp_path / "openrouter_api_key").write_text(KEY)

    secrets = GatewaySecrets(_secrets_dir=tmp_path)

    assert secrets.openrouter_api_key.get_secret_value() == KEY


def test_a_caller_hanging_up_mid_stream_is_recorded_as_incomplete(
    tmp_path: Path,
    served_gateway: str,
) -> None:
    with httpx2.stream("POST", served_gateway + CHAT, json=BODY) as reply:
        next(reply.iter_raw())

    [call] = wait_for_calls(tmp_path)
    assert (call.response, call.complete) == (STREAM[:20].decode(), False)
