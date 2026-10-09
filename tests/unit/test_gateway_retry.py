"""The OpenRouter provider retries a rate-limited or transient response with backoff, then gives up loudly.

A burst of calls against a rate-limited model must not abort the whole episode on the first 429, but a
persistent rate limit or server error still raises after a bounded number of retries.
"""

from __future__ import annotations

import httpx2
import pytest
from loc_arena.gateway import core

_REQUEST = core.request_body("some/model", [{"role": "user", "content": "hi"}], 0.0, 16, None)


def _response(status: int, *, content: str = "ok") -> httpx2.Response:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return httpx2.Response(status_code=status, json=body, request=httpx2.Request("POST", core.OPENROUTER_URL))


def test_retries_a_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    replies = [_response(429), _response(429), _response(200, content="done")]

    def fake_post(*_args: object, **_kwargs: object) -> httpx2.Response:
        calls.append(1)
        return replies.pop(0)

    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", fake_post)
    monkeypatch.setattr("loc_arena.gateway.core.time.sleep", lambda _s: None)  # no real waiting
    provider = core.OpenRouterProvider(api_key="test-key")

    result = provider.complete(_REQUEST)

    assert result["choices"][0]["message"]["content"] == "done"
    assert len(calls) == 3  # two 429s were retried, the third succeeded


def test_persistent_429_raises_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def always_429(*_args: object, **_kwargs: object) -> httpx2.Response:
        calls.append(1)
        return _response(429)

    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", always_429)
    monkeypatch.setattr("loc_arena.gateway.core.time.sleep", lambda _s: None)
    provider = core.OpenRouterProvider(api_key="test-key")

    with pytest.raises(httpx2.HTTPStatusError):
        provider.complete(_REQUEST)

    assert len(calls) == core._MAX_RETRIES + 1  # one initial attempt plus the retry budget


def test_retry_delay_honors_retry_after_header() -> None:
    resp = httpx2.Response(
        status_code=429,
        headers={"Retry-After": "7"},
        request=httpx2.Request("POST", core.OPENROUTER_URL),
    )
    assert core._retry_delay_seconds(resp, attempt=0) == 7.0
    # a missing header falls back to exponential backoff, capped at 30s
    bare = httpx2.Response(status_code=429, request=httpx2.Request("POST", core.OPENROUTER_URL))
    assert core._retry_delay_seconds(bare, attempt=0) == core._BACKOFF_BASE_SECONDS
    assert core._retry_delay_seconds(bare, attempt=10) == 30.0
