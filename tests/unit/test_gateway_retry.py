"""The OpenRouter provider retries a server error or a rate limit, then gives up loudly within its budget.

A burst of calls against a rate-limited model must not abort the episode on the first 429, but a persistent
rate limit or server error still raises, and no Retry-After can stretch a call past its budget. Every test
talks to a local stub (tests/unit/_openrouter_stub.py) with a fake key.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from http import HTTPStatus

import pytest
import stamina
from loc_arena.gateway.openrouter_provider import ProviderError, ProviderTimeoutError

from tests.unit._openrouter_stub import (
    ScriptedReply,
    completion,
    error_body,
    fast_settings,
    generate,
    serve_openrouter,
)

RETRIED_TEXT = "done"
BUDGET_MILLISECONDS = 1_000
MILLISECONDS_PER_SECOND = 1_000
DEADLINE_SECONDS = 0.3
RETRY_BUDGET_MILLISECONDS = 3_000  # room for a request cut at its deadline plus the retry that succeeds
BUDGET_MARGIN_SECONDS = 0.5
RETRY_AFTER_ABOVE_THE_CAP = "7"  # seconds: far above fast_settings' cap, so waiting it would show
RETRY_AFTER_BELOW_THE_CAP_SECONDS = 0.005
RETRY_AFTER_ONE_HOUR = "3600"
PERSISTENT_RATE_LIMIT_ATTEMPTS = 3
UNAVAILABLE = ScriptedReply(
    HTTPStatus.SERVICE_UNAVAILABLE,
    error_body(HTTPStatus.SERVICE_UNAVAILABLE, "No available provider"),
)
RETRIED_COMPLETION = ScriptedReply(body=completion(RETRIED_TEXT))


@pytest.fixture
def retry_waits() -> Iterator[list[float]]:
    waits: list[float] = []
    stamina.instrumentation.set_on_retry_hooks([lambda details: waits.append(details.wait_for)])
    yield waits
    stamina.instrumentation.set_on_retry_hooks(None)


def _rate_limited(headers: Mapping[str, str]) -> ScriptedReply:
    return ScriptedReply(
        HTTPStatus.TOO_MANY_REQUESTS,
        error_body(HTTPStatus.TOO_MANY_REQUESTS, "Rate limit exceeded"),
        headers,
    )


def test_a_server_error_followed_by_a_completion_returns_the_completion() -> None:
    with serve_openrouter(UNAVAILABLE, RETRIED_COMPLETION) as stub:
        result = generate(stub)

    assert result.text == RETRIED_TEXT


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (HTTPStatus.UNAUTHORIZED, "No auth credentials found"),
        (HTTPStatus.BAD_REQUEST, "Input must have at least 1 token"),  # what an empty prompt gets
    ],
)
def test_a_refusal_other_than_a_rate_limit_is_not_retried(status: HTTPStatus, message: str) -> None:
    refusal = error_body(status, message)

    with (
        serve_openrouter(ScriptedReply(status, refusal)) as stub,
        pytest.raises(ProviderError),
    ):
        generate(stub)

    assert len(stub.received) == 1


def test_a_rate_limit_followed_by_a_completion_returns_the_completion() -> None:
    rate_limit = _rate_limited({"Retry-After": RETRY_AFTER_ABOVE_THE_CAP})

    with serve_openrouter(rate_limit, RETRIED_COMPLETION) as stub:
        result = generate(stub)

    assert result.text == RETRIED_TEXT


def test_a_retry_after_above_the_cap_is_waited_as_the_cap(retry_waits: list[float]) -> None:
    rate_limit = _rate_limited({"Retry-After": RETRY_AFTER_ABOVE_THE_CAP})

    with serve_openrouter(rate_limit, RETRIED_COMPLETION) as stub:
        settings = fast_settings(stub)
        generate(stub, settings)

    assert retry_waits == [pytest.approx(settings.rate_limit_max_wait_seconds)]


def test_a_retry_after_below_the_cap_is_waited_as_given(retry_waits: list[float]) -> None:
    rate_limit = _rate_limited({"Retry-After": str(RETRY_AFTER_BELOW_THE_CAP_SECONDS)})

    with serve_openrouter(rate_limit, RETRIED_COMPLETION) as stub:
        generate(stub)

    assert retry_waits == [pytest.approx(RETRY_AFTER_BELOW_THE_CAP_SECONDS)]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"Retry-After": "-1"}, {"Retry-After": "nan"}],
    ids=["absent", "http-date", "negative", "not-a-number"],
)
def test_a_rate_limit_without_a_usable_retry_after_is_retried_after_a_capped_backoff(
    headers: Mapping[str, str],
    retry_waits: list[float],
) -> None:
    with serve_openrouter(_rate_limited(headers), RETRIED_COMPLETION) as stub:
        settings = fast_settings(stub)
        generate(stub, settings)

    (wait,) = retry_waits
    assert 0 < wait <= settings.rate_limit_max_wait_seconds


def test_a_persistent_rate_limit_raises_a_provider_error() -> None:
    rate_limit = _rate_limited({"Retry-After": RETRY_AFTER_ABOVE_THE_CAP})

    with serve_openrouter(rate_limit) as stub, pytest.raises(ProviderError, match="Rate limit exceeded"):
        generate(stub)


def test_a_persistent_rate_limit_is_attempted_as_often_as_configured() -> None:
    rate_limit = _rate_limited({"Retry-After": RETRY_AFTER_ABOVE_THE_CAP})

    with serve_openrouter(rate_limit) as stub:
        attempts = {"rate_limit_attempts": PERSISTENT_RATE_LIMIT_ATTEMPTS}
        settings = fast_settings(stub).model_copy(update=attempts)

        with pytest.raises(ProviderError):
            generate(stub, settings)

    assert len(stub.received) == PERSISTENT_RATE_LIMIT_ATTEMPTS


def test_a_request_cut_by_its_deadline_is_retried() -> None:
    with serve_openrouter(ScriptedReply(trickle=True), RETRIED_COMPLETION) as stub:
        settings = fast_settings(stub).model_copy(
            update={
                "request_deadline_seconds": DEADLINE_SECONDS,
                "retry_connection_errors": True,
                "backoff_max_elapsed_time_milliseconds": RETRY_BUDGET_MILLISECONDS,
            },
        )
        result = generate(stub, settings)

    assert result.text == RETRIED_TEXT


def test_no_retry_after_stretches_the_call_past_its_budget() -> None:
    retry_after_one_hour = {"Retry-After": RETRY_AFTER_ONE_HOUR}
    unavailable_for_an_hour = ScriptedReply(UNAVAILABLE.status, UNAVAILABLE.body, retry_after_one_hour)
    budget_seconds = BUDGET_MILLISECONDS / MILLISECONDS_PER_SECOND

    with serve_openrouter(unavailable_for_an_hour) as stub:
        budget = {"backoff_max_elapsed_time_milliseconds": BUDGET_MILLISECONDS}
        settings = fast_settings(stub).model_copy(update=budget)
        started = time.monotonic()

        with pytest.raises(ProviderTimeoutError, match="budget"):
            generate(stub, settings)
        elapsed = time.monotonic() - started

    assert budget_seconds <= elapsed < budget_seconds + BUDGET_MARGIN_SECONDS
