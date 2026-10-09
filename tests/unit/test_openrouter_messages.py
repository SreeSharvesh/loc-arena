"""The OpenRouter provider sends the full message list and tools verbatim and returns native tool calls."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from loc_arena.gateway import core

TIMEOUT_SECONDS = 1.0  # stubbed posts never wait

TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
MESSAGES = [{"role": "system", "content": "be terse"}, {"role": "user", "content": "list files"}]
ESTIMATED_PROMPT_TOKENS = 21  # the 86 characters of MESSAGES as JSON, at about 4 per token


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
    *,
    usage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    reported = usage if usage is not None else {"prompt_tokens": 3, "completion_tokens": 2}

    def fake_post(*_args: object, json: dict[str, Any], **_kwargs: object) -> httpx2.Response:
        bodies.append(json)
        return httpx2.Response(
            status_code=200,
            json={"choices": [{"message": message}], "usage": reported},
            request=httpx2.Request("POST", core.OPENROUTER_URL),
        )

    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", fake_post)
    return bodies


def test_sends_messages_and_tools_and_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture(monkeypatch, {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]})

    result = core.OpenRouterProvider(api_key="k", timeout=TIMEOUT_SECONDS).generate(
        "m",
        MESSAGES,
        0.0,
        16,
        TOOLS,
    )

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

    result = core.OpenRouterProvider(api_key="k", timeout=TIMEOUT_SECONDS).generate(
        "m",
        MESSAGES,
        0.0,
        16,
        None,
    )

    assert result.prompt_tokens == 11
    assert result.completion_tokens == 4
    assert result.cached_tokens == 7


def test_omits_tools_and_tool_calls_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture(monkeypatch, {"role": "assistant", "content": "done"})

    result = core.OpenRouterProvider(api_key="k", timeout=TIMEOUT_SECONDS).generate(
        "m",
        MESSAGES,
        0.0,
        16,
        None,
    )

    assert "tools" not in bodies[0]
    assert result.tool_calls is None
    assert result.assistant_message() == {"role": "assistant", "content": "done"}


@pytest.mark.parametrize(
    "body",
    [
        b"<html>upstream error</html>",
        b'{"error": {"message": "upstream error", "code": 502}}',
        b'{"choices": []}',
        b'{"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]}',
        b'{"choices": [{"message": {"content": null, "tool_calls": {"id": "call_1"}}}]}',
        b'{"choices": [{"message": {"content": null, "tool_calls": ["call_1"]}}]}',
        b'{"choices": [{"finish_reason": "stop"}]}',
        b'{"choices": [{"message": {"content": ""}, "finish_reason": "error"}]}',
    ],
    ids=[
        "not JSON",
        "an error body",
        "no choices",
        "non-text content",
        "tool calls not a list",
        "a tool call not an object",
        "no message",
        "an error mid-reply",
    ],
)
def test_a_reply_that_is_no_chat_completion_raises_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    reply = httpx2.Response(
        status_code=200,
        content=body,
        request=httpx2.Request("POST", core.OPENROUTER_URL),
    )
    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", lambda *_args, **_kwargs: reply)
    provider = core.OpenRouterProvider(api_key="k", timeout=TIMEOUT_SECONDS)

    with pytest.raises(core.ProviderError):
        provider.generate("m", MESSAGES, 0.0, 16, TOOLS)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": None, "completion_tokens": 4}, (ESTIMATED_PROMPT_TOKENS, 4, 0)),
        (
            {"prompt_tokens": 11, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": None}},
            (11, 4, 0),
        ),
    ],
    ids=["null prompt tokens", "null cached tokens"],
)
def test_a_null_token_count_reads_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, Any],
    expected: tuple[int, int, int],
) -> None:
    _capture(monkeypatch, {"role": "assistant", "content": "done"}, usage=usage)

    result = core.OpenRouterProvider(api_key="k", timeout=TIMEOUT_SECONDS).generate(
        "m",
        MESSAGES,
        0.0,
        16,
        None,
    )

    assert (result.prompt_tokens, result.completion_tokens, result.cached_tokens) == expected
