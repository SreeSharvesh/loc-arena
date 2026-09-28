"""The OpenRouter provider sends the full message list and tools verbatim and returns native tool calls."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from loc_arena.gateway.openrouter_provider import OpenRouterProvider
from pydantic import SecretStr

from tests.unit._openrouter_stub import (
    CANARY_KEY,
    ScriptedReply,
    StubOpenRouter,
    completion,
    fast_settings,
    serve_openrouter,
)

TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
MESSAGES = [{"role": "system", "content": "be terse"}, {"role": "user", "content": "list files"}]


class _RequestBodies:
    """The JSON bodies of the requests a stub has received, read when accessed, so after the call."""

    def __init__(self, stub: StubOpenRouter) -> None:
        self._stub = stub

    def __len__(self) -> int:
        return len(self._stub.received)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return json.loads(self._stub.received[index].body)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return (json.loads(request.body) for request in self._stub.received)


@pytest.fixture
def stub() -> Iterator[StubOpenRouter]:
    """A local OpenRouter on loopback; ``_capture`` sets the completion it answers."""
    with serve_openrouter(ScriptedReply(body=completion())) as server:
        yield server


def _provider(stub: StubOpenRouter) -> OpenRouterProvider:
    return OpenRouterProvider(fast_settings(stub), SecretStr(CANARY_KEY))


def _capture(
    stub: StubOpenRouter,
    message: dict[str, Any],
    *,
    usage: dict[str, Any] | None = None,
) -> _RequestBodies:
    reported = usage if usage is not None else {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    reply = completion(message["content"], tool_calls=message.get("tool_calls"), usage=reported)
    stub.pending_replies = [ScriptedReply(body=reply)]
    return _RequestBodies(stub)


def test_sends_messages_and_tools_and_parses_tool_calls(stub: StubOpenRouter) -> None:
    bodies = _capture(stub, {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]})

    result = _provider(stub).generate("m", MESSAGES, 0.0, 16, TOOLS)

    (body,) = bodies
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert result.text == ""
    assert result.tool_calls == [TOOL_CALL]
    assert result.assistant_message() == {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}


def test_records_cached_tokens(stub: StubOpenRouter) -> None:
    _capture(
        stub,
        {"role": "assistant", "content": "done"},
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 7},
        },
    )

    result = _provider(stub).generate("m", MESSAGES, 0.0, 16, None)

    assert result.prompt_tokens == 11
    assert result.completion_tokens == 4
    assert result.cached_tokens == 7


def test_omits_tools_and_tool_calls_when_absent(stub: StubOpenRouter) -> None:
    bodies = _capture(stub, {"role": "assistant", "content": "done"})

    result = _provider(stub).generate("m", MESSAGES, 0.0, 16, None)

    assert "tools" not in bodies[0]
    assert result.tool_calls is None
    assert result.assistant_message() == {"role": "assistant", "content": "done"}
