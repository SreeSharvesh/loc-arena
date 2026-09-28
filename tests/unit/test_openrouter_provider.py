"""The OpenRouter provider sends one chat completion, reads it strictly, and cannot outlast its deadline."""

from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from pathlib import Path

import pytest
from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.openrouter_provider import (
    OpenRouterProvider,
    ProviderError,
    ProviderReplyError,
    ProviderTimeoutError,
    live_provider_from_environment,
)
from loc_arena.stack.constants import CONTROL_KEY_SECRET_NAME, OPENROUTER_API_KEY_SECRET_NAME
from loc_arena.stack.settings import ProviderSettings

from tests.unit._openrouter_stub import (
    CANARY_KEY,
    MAX_TOKENS,
    MESSAGES,
    MODEL,
    PROMPT,
    TEMPERATURE,
    ScriptedReply,
    StubOpenRouter,
    completion,
    error_body,
    fast_settings,
    generate,
    serve_openrouter,
    stub_provider,
)

DEADLINE_SECONDS = 0.5
PHASE_TIMEOUT_MILLISECONDS = 300  # above the stub's 100 ms between bytes: httpx's read timeout never trips
DEADLINE_MARGIN_SECONDS = 0.5
TEXT = "hello"
DOTENV_FILE_NAME = ".env"
SDK_DEBUG_ENVIRONMENT_VARIABLE = "OPENROUTER_DEBUG"
SDK_LOGGER_NAME = "openrouter"
COST = 0.0042  # credits


def _completion_costing(cost: float | None) -> str:
    body = json.loads(completion())
    body["usage"]["cost"] = cost
    return json.dumps(body)


def _completion_without_choices() -> str:
    body = json.loads(completion())
    body["choices"] = []
    return json.dumps(body)


def _completion_with_content_parts() -> str:
    body = json.loads(completion())
    body["choices"][0]["message"]["content"] = [{"type": "text", "text": TEXT}]
    return json.dumps(body)


UNUSABLE_COMPLETIONS = pytest.mark.parametrize(
    ("body", "reason"),
    [
        (error_body(HTTPStatus.BAD_GATEWAY, "Provider disconnected"), "error 502: Provider disconnected"),
        (json.dumps({"id": "gen-1"}), "not a chat completion"),
        (_completion_without_choices(), "without a choice"),
        (completion(finish_reason="error"), "finish_reason 'error'"),
        (completion(usage=None), "without token usage"),
        (_completion_with_content_parts(), "content parts, not text"),
    ],
    ids=["error-body", "no-completion", "no-choice", "failed-generation", "no-usage", "content-parts"],
)


@pytest.fixture(autouse=True)
def _no_secret_in_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (OPENROUTER_API_KEY_SECRET_NAME, CONTROL_KEY_SECRET_NAME):
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)  # a stray ./.env is never read unless asked for


def _deadline_settings(stub: StubOpenRouter) -> ProviderSettings:
    return fast_settings(stub).model_copy(
        update={
            "request_timeout_milliseconds": PHASE_TIMEOUT_MILLISECONDS,
            "request_deadline_seconds": DEADLINE_SECONDS,
            "retry_connection_errors": False,
        },
    )


def _call(provider: OpenRouterProvider | None) -> None:
    if provider is None:
        pytest.fail("no provider: the key was not found")
    provider.generate(MODEL, MESSAGES, TEMPERATURE, MAX_TOKENS, None)


def test_a_completion_is_read_into_its_text_and_token_usage() -> None:
    body = completion(TEXT)
    usage = json.loads(body)["usage"]

    with serve_openrouter(ScriptedReply(body=body)) as stub:
        result = generate(stub)

    assert result == ProviderResult(
        text=TEXT,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
    )


def test_a_completion_reports_the_cost_openrouter_charged() -> None:
    with serve_openrouter(ScriptedReply(body=_completion_costing(COST))) as stub:
        result = generate(stub)

    assert result.cost == COST


@pytest.mark.parametrize("body", [completion(), _completion_costing(None)], ids=["absent", "null"])
def test_a_completion_without_a_cost_reports_its_cost_unknown(body: str) -> None:
    with serve_openrouter(ScriptedReply(body=body)) as stub:
        result = generate(stub)

    assert result.cost is None


def test_a_request_carries_the_key_as_a_bearer_token() -> None:
    with serve_openrouter(ScriptedReply(body=completion())) as stub:
        generate(stub)

    (request,) = stub.received
    assert request.authorization == f"Bearer {CANARY_KEY}"


def test_a_prompt_is_sent_as_one_user_message_with_the_call_parameters() -> None:
    with serve_openrouter(ScriptedReply(body=completion())) as stub:
        generate(stub)

    (request,) = stub.received
    assert json.loads(request.body) == {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": TEMPERATURE,
        "max_completion_tokens": MAX_TOKENS,
        "stream": False,
    }


def test_a_null_content_reads_as_empty_text() -> None:
    with serve_openrouter(ScriptedReply(body=completion(None, finish_reason="length"))) as stub:
        result = generate(stub)

    assert result.text == ""


def test_a_message_list_the_sdk_cannot_send_raises_a_provider_error() -> None:
    tool_result_without_its_call_id = [{"role": "tool", "content": TEXT}]

    with serve_openrouter(ScriptedReply(body=completion())) as stub, pytest.raises(ProviderError):
        stub_provider(stub).generate(MODEL, tool_result_without_its_call_id, TEMPERATURE, MAX_TOKENS, None)


CACHE_BREAKPOINT = {"type": "ephemeral"}


def test_a_cache_breakpoint_on_a_content_part_reaches_openrouter() -> None:
    messages = [
        {"role": "system", "content": [{"type": "text", "text": TEXT, "cache_control": CACHE_BREAKPOINT}]},
        {"role": "user", "content": [{"type": "text", "text": PROMPT, "cache_control": CACHE_BREAKPOINT}]},
    ]

    with serve_openrouter(ScriptedReply(body=completion())) as stub:
        stub_provider(stub).generate(MODEL, messages, TEMPERATURE, MAX_TOKENS, None)

    (request,) = stub.received
    assert json.loads(request.body)["messages"] == messages


def test_a_cache_breakpoint_on_a_tool_reaches_openrouter() -> None:
    function = {"name": "list_dir", "parameters": {"type": "object"}}
    tools = [{"type": "function", "function": function, "cache_control": CACHE_BREAKPOINT}]

    with serve_openrouter(ScriptedReply(body=completion())) as stub:
        stub_provider(stub).generate(MODEL, MESSAGES, TEMPERATURE, MAX_TOKENS, tools)

    (request,) = stub.received
    assert json.loads(request.body)["tools"] == tools


@UNUSABLE_COMPLETIONS
def test_a_200_without_a_usable_completion_is_not_retried(body: str, reason: str) -> None:
    with serve_openrouter(ScriptedReply(body=body)) as stub, pytest.raises(ProviderReplyError, match=reason):
        generate(stub)

    assert len(stub.received) == 1


def test_a_trickled_reply_raises_a_timeout_at_the_request_deadline() -> None:
    with serve_openrouter(ScriptedReply(trickle=True)) as stub:
        settings = _deadline_settings(stub)
        started = time.monotonic()

        with pytest.raises(ProviderTimeoutError, match="request deadline"):
            generate(stub, settings)
        elapsed = time.monotonic() - started

    assert DEADLINE_SECONDS <= elapsed < DEADLINE_SECONDS + DEADLINE_MARGIN_SECONDS


def test_a_request_cut_at_its_deadline_closes_its_connection() -> None:
    with serve_openrouter(ScriptedReply(trickle=True)) as stub, pytest.raises(ProviderTimeoutError):
        generate(stub, _deadline_settings(stub))

    assert stub.client_hung_up.is_set()


def test_a_call_cut_at_its_deadline_leaves_no_thread_behind() -> None:
    threads_before = set(threading.enumerate())

    with serve_openrouter(ScriptedReply(trickle=True)) as stub, pytest.raises(ProviderTimeoutError):
        generate(stub, _deadline_settings(stub))

    assert set(threading.enumerate()) == threads_before


def test_the_key_never_shows_in_the_provider_or_its_error() -> None:
    unauthorized = error_body(HTTPStatus.UNAUTHORIZED, "No auth credentials found")

    with serve_openrouter(ScriptedReply(HTTPStatus.UNAUTHORIZED, unauthorized)) as stub:
        provider = stub_provider(stub)
        with pytest.raises(ProviderError) as raised:
            provider.generate(MODEL, MESSAGES, TEMPERATURE, MAX_TOKENS, None)

    shown = [repr(provider), str(raised.value), repr(raised.value), repr(raised.value.__cause__)]
    assert [text for text in shown if CANARY_KEY in text] == []


def test_the_sdk_debug_log_of_a_call_never_shows_the_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(SDK_DEBUG_ENVIRONMENT_VARIABLE, "1")
    caplog.set_level(logging.DEBUG, logger=SDK_LOGGER_NAME)

    with serve_openrouter(ScriptedReply(body=completion())) as stub:
        generate(stub)

    assert "authorization" in caplog.text.lower()  # the log holds the request headers, so it could leak
    assert CANARY_KEY not in caplog.text


def test_the_live_provider_sends_the_key_from_a_dotenv_file(tmp_path: Path) -> None:
    dotenv = tmp_path / "keys.env"
    dotenv.write_text(f"{OPENROUTER_API_KEY_SECRET_NAME.upper()}={CANARY_KEY}\n")

    with serve_openrouter(ScriptedReply(body=completion())) as stub:
        _call(live_provider_from_environment(fast_settings(stub), dotenv_path=dotenv))

    (request,) = stub.received
    assert request.authorization == f"Bearer {CANARY_KEY}"


def test_the_live_provider_ignores_a_dotenv_file_it_is_not_given(tmp_path: Path) -> None:
    (tmp_path / DOTENV_FILE_NAME).write_text(f"{OPENROUTER_API_KEY_SECRET_NAME.upper()}={CANARY_KEY}\n")

    provider = live_provider_from_environment(ProviderSettings())

    assert provider is None


def test_the_live_provider_is_none_when_its_dotenv_file_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"

    provider = live_provider_from_environment(ProviderSettings(), dotenv_path=missing)

    assert provider is None
