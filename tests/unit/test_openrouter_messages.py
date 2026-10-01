"""The OpenRouter provider sends the full message list and tools verbatim and returns native tool calls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from loc_arena.gateway import core

TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
MESSAGES = [{"role": "system", "content": "be terse"}, {"role": "user", "content": "list files"}]


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
    *,
    usage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    reported = usage if usage is not None else {"prompt_tokens": 3, "completion_tokens": 2}

    def fake_post(*_args: object, json: dict[str, Any], **_kwargs: object) -> httpx.Response:
        bodies.append(json)
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": message}], "usage": reported},
            request=httpx.Request("POST", core.OPENROUTER_URL),
        )

    monkeypatch.setattr("loc_arena.gateway.core.httpx.post", fake_post)
    return bodies


def test_sends_messages_and_tools_and_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture(monkeypatch, {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]})

    result = core.OpenRouterProvider(api_key="k").generate("m", MESSAGES, 0.0, 16, TOOLS)

    (body,) = bodies
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert result.text == ""
    assert result.tool_calls == [TOOL_CALL]
    assert result.assistant_message() == {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}


def test_records_cached_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(
        monkeypatch,
        {"role": "assistant", "content": "done"},
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 7},
        },
    )

    result = core.OpenRouterProvider(api_key="k").generate("m", MESSAGES, 0.0, 16, None)

    assert result.prompt_tokens == 11
    assert result.completion_tokens == 4
    assert result.cached_tokens == 7


def test_omits_tools_and_tool_calls_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture(monkeypatch, {"role": "assistant", "content": "done"})

    result = core.OpenRouterProvider(api_key="k").generate("m", MESSAGES, 0.0, 16, None)

    assert "tools" not in bodies[0]
    assert result.tool_calls is None
    assert result.assistant_message() == {"role": "assistant", "content": "done"}
