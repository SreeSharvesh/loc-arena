"""The gateway proxy swaps in the real key, records every call with its caller, and passes replies through.

A caller sends an OpenRouter request with any key; the upstream sees only the gateway's key, the reply comes
back unchanged (a streamed one chunk by chunk), and the call log holds the caller, the request and the reply,
never the key. A provider pointed at the gateway holds no key at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from loc_arena.gateway import core, proxy
from pydantic import SecretStr

KEY = "sk-real-key-only-the-gateway-holds"
UPSTREAM = "https://upstream.test"
COMPLETION = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}
STREAM = b'data: {"choices":[{"delta":{"content":"h"}}]}\n\ndata: {"choices":[{"delta":{"content":"i"}}]}\n\n'


@pytest.fixture(autouse=True)
def _caller_is_the_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy.socket, "gethostbyaddr", lambda _addr: ("episode", [], []))


def _gateway(tmp_path: Path, handler: Any) -> tuple[TestClient, Path]:
    call_log = tmp_path / "calls.jsonl"
    client = httpx.AsyncClient(base_url=UPSTREAM, transport=httpx.MockTransport(handler))
    return TestClient(proxy.create_proxy_app(UPSTREAM, SecretStr(KEY), call_log, client=client)), call_log


def _calls(call_log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in call_log.read_text().splitlines()]


def test_the_upstream_sees_the_gateway_key_and_the_caller_gets_the_reply(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=COMPLETION)

    gateway, call_log = _gateway(tmp_path, upstream)
    body = {"model": "some/model", "messages": [{"role": "user", "content": "hello"}]}
    caller_key = {"Authorization": "Bearer caller-key"}
    reply = gateway.post("/api/v1/chat/completions", json=body, headers=caller_key)

    assert reply.status_code == 200 and reply.json() == COMPLETION
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert str(seen[0].url) == f"{UPSTREAM}/api/v1/chat/completions"
    [call] = _calls(call_log)
    assert call["caller"] == "episode" and call["status"] == 200
    assert call["request"] == body and call["response"] == COMPLETION
    assert KEY not in call_log.read_text()


def test_a_caller_cannot_choose_the_upstream_host(tmp_path: Path) -> None:
    hosts: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json=COMPLETION)

    gateway, _ = _gateway(tmp_path, upstream)
    gateway.post("//elsewhere.test/api/v1/chat/completions", json={}, headers={"Host": "elsewhere.test"})

    assert hosts == ["upstream.test"]


def test_a_streamed_reply_passes_through_and_is_recorded(tmp_path: Path) -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    gateway, call_log = _gateway(tmp_path, upstream)
    with gateway.stream("POST", "/api/v1/chat/completions", json={"stream": True}) as reply:
        received = b"".join(reply.iter_bytes())

    assert received == STREAM
    assert reply.headers["content-type"].startswith("text/event-stream")
    [call] = _calls(call_log)
    assert call["response"] == STREAM.decode()


def test_an_unreachable_upstream_is_a_recorded_502(tmp_path: Path) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gateway, call_log = _gateway(tmp_path, upstream)
    reply = gateway.post("/api/v1/chat/completions", json={"model": "m"})

    assert reply.status_code == 502
    [call] = _calls(call_log)
    assert call["status"] == 502 and call["request"] == {"model": "m"}


def test_a_provider_behind_the_gateway_holds_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(core.GATEWAY_URL_ENV, "http://gateway:8080/api/v1/chat/completions")
    posted: list[tuple[str, dict[str, str]]] = []

    def fake_post(url: str, *, headers: dict[str, str], **_kwargs: object) -> httpx.Response:
        posted.append((url, headers))
        return httpx.Response(200, json=COMPLETION, request=httpx.Request("POST", url))

    monkeypatch.setattr("loc_arena.gateway.core.httpx.post", fake_post)

    assert core.live_provider_configured()
    result = core.OpenRouterProvider().generate("m", [{"role": "user", "content": "hi"}], 0.0, 8, None)

    assert result.text == "hi"
    [(url, headers)] = posted
    assert url == "http://gateway:8080/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer held-by-the-gateway"  # a placeholder the gateway replaces


def test_no_gateway_and_no_key_is_no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv(core.GATEWAY_URL_ENV, raising=False)

    assert not core.live_provider_configured()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        core.OpenRouterProvider()
