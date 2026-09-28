"""The openrouter SDK, on pyproject's overridden pydantic, still behaves as loc_arena reads it."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus

import httpx
import pytest
from loc_arena.gateway.core import Message, ToolSpec
from loc_arena.scaffold.tool_specs import agent_tool_specs
from openrouter import OpenRouter, components, errors

MODEL = "vendor/model"
PROMPT = "hi"
TEXT = "hello"
TEMPERATURE = 0.0
MAX_COMPLETION_TOKENS = 5
RETRY_AFTER_SECONDS = "7"
USAGE = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
COMPLETION = {
    "id": "gen-1",
    "object": "chat.completion",
    "created": 1,
    "model": MODEL,
    "system_fingerprint": None,
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": TEXT}}],
    "usage": USAGE,
}
NOT_A_COMPLETION = json.dumps({"id": "gen-1"})
CACHED_TOKENS = 2
TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "list_dir", "arguments": '{"path": "."}'},
}
# Every message kind an agent's loop sends: system, user, an assistant's tool call and the call's result.
HISTORY = [
    {"role": "system", "content": "be terse"},
    {"role": "user", "content": PROMPT},
    {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]},
    {"role": "tool", "tool_call_id": TOOL_CALL["id"], "content": "a.py"},
]
TOOL_CALL_COMPLETION = {
    **COMPLETION,
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
        },
    ],
    "usage": {**USAGE, "prompt_tokens_details": {"cached_tokens": CACHED_TOKENS}},
}
JSON_CONTENT_TYPE = {"Content-Type": "application/json"}


def _send(
    reply: httpx.Response,
    sent: list[httpx.Request],
    *,
    messages: list[Message] | None = None,
    tools: list[ToolSpec] | None = None,
) -> components.ChatResult:
    """Send one chat call as the provider does; the transport keeps it in ``sent`` and answers ``reply``.

    ``messages`` defaults to the one user message ``PROMPT``; ``tools`` left ``None`` are left out.
    """

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return reply

    async def send() -> components.ChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http_client:
            sdk = OpenRouter(api_key="test-key", async_client=http_client)
            return await sdk.chat.send_async(
                model=MODEL,
                messages=messages or [{"role": "user", "content": PROMPT}],
                tools=tools,
                temperature=TEMPERATURE,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                stream=False,
            )

    return asyncio.run(send())


def _completion_reply() -> httpx.Response:
    return httpx.Response(HTTPStatus.OK, json=COMPLETION)


def _rate_limit_reply() -> httpx.Response:
    return httpx.Response(
        HTTPStatus.TOO_MANY_REQUESTS,
        json={"error": {"code": HTTPStatus.TOO_MANY_REQUESTS, "message": "slow down"}},
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


def test_the_sdk_sends_a_chat_call_without_its_unset_optional_fields() -> None:
    sent: list[httpx.Request] = []

    _send(_completion_reply(), sent)

    (request,) = sent
    assert json.loads(request.content) == {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": TEMPERATURE,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "stream": False,
    }


def test_the_sdk_sends_a_message_list_and_tools_as_given() -> None:
    sent: list[httpx.Request] = []
    tools = agent_tool_specs(covert=True)

    _send(_completion_reply(), sent, messages=HISTORY, tools=tools)

    (request,) = sent
    body = json.loads(request.content)
    assert (body["messages"], body["tools"]) == (HISTORY, tools)


def test_the_sdk_parses_tool_calls_and_cached_tokens() -> None:
    reply = httpx.Response(HTTPStatus.OK, json=TOOL_CALL_COMPLETION)

    result = _send(reply, [])

    tool_calls = result.choices[0].message.tool_calls or []
    details = result.usage.prompt_tokens_details if result.usage else None
    cached_tokens = details.cached_tokens if details else None
    assert ([call.model_dump() for call in tool_calls], cached_tokens) == ([TOOL_CALL], CACHED_TOKENS)


def test_the_sdk_parses_the_first_choice_of_a_chat_completion() -> None:
    result = _send(_completion_reply(), [])

    assert (result.choices[0].message.content, result.choices[0].finish_reason) == (TEXT, "stop")


def test_the_sdk_parses_the_token_usage_of_a_chat_completion() -> None:
    result = _send(_completion_reply(), [])

    assert result.usage is not None
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (
        USAGE["prompt_tokens"],
        USAGE["completion_tokens"],
    )


def test_a_rate_limit_raises_an_openrouter_error_with_status_429() -> None:
    with pytest.raises(errors.OpenRouterError) as raised:
        _send(_rate_limit_reply(), [])

    assert raised.value.status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_a_rate_limit_error_carries_its_retry_after_header_under_the_lowercase_name() -> None:
    with pytest.raises(errors.OpenRouterError) as raised:
        _send(_rate_limit_reply(), [])

    assert raised.value.headers.get("retry-after") == RETRY_AFTER_SECONDS


def test_a_200_that_is_no_chat_completion_raises_a_validation_error_carrying_the_reply() -> None:
    reply = httpx.Response(HTTPStatus.OK, content=NOT_A_COMPLETION.encode(), headers=JSON_CONTENT_TYPE)

    with pytest.raises(errors.ResponseValidationError) as raised:
        _send(reply, [])

    assert (raised.value.status_code, raised.value.body) == (HTTPStatus.OK, NOT_A_COMPLETION)
