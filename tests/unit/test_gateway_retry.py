"""The OpenRouter provider retries a rate-limited or transient response with backoff, then gives up loudly.

A burst of calls against a rate-limited model must not abort the whole episode on the first 429, but a
persistent rate limit or server error still raises after a bounded number of retries.
"""

from __future__ import annotations

import httpx
import pytest
from loc_arena.gateway import core


def _response(status: int, *, content: str = "ok") -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return httpx.Response(status_code=status, json=body, request=httpx.Request("POST", core.OPENROUTER_URL))


def test_retries_a_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    replies = [_response(429), _response(429), _response(200, content="done")]

    def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        calls.append(1)
        return replies.pop(0)

    monkeypatch.setattr("loc_arena.gateway.core.httpx.post", fake_post)
    monkeypatch.setattr("loc_arena.gateway.core.time.sleep", lambda _s: None)  # no real waiting
    provider = core.OpenRouterProvider(api_key="test-key")

    result = provider.generate("some/model", "hi", 0.0, 16)

    assert result.text == "done"
    assert len(calls) == 3  # two 429s were retried, the third succeeded


def test_persistent_429_raises_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def always_429(*_args: object, **_kwargs: object) -> httpx.Response:
        calls.append(1)
        return _response(429)

    monkeypatch.setattr("loc_arena.gateway.core.httpx.post", always_429)
    monkeypatch.setattr("loc_arena.gateway.core.time.sleep", lambda _s: None)
    provider = core.OpenRouterProvider(api_key="test-key")

    with pytest.raises(httpx.HTTPStatusError):
        provider.generate("some/model", "hi", 0.0, 16)

    assert len(calls) == core._MAX_RETRIES + 1  # one initial attempt plus the retry budget


def test_retry_delay_honors_retry_after_header() -> None:
    resp = httpx.Response(
        status_code=429,
        headers={"Retry-After": "7"},
        request=httpx.Request("POST", core.OPENROUTER_URL),
    )
    assert core._retry_delay_seconds(resp, attempt=0) == 7.0
    # a missing header falls back to exponential backoff, capped at 30s
    bare = httpx.Response(status_code=429, request=httpx.Request("POST", core.OPENROUTER_URL))
    assert core._retry_delay_seconds(bare, attempt=0) == core._BACKOFF_BASE_SECONDS
    assert core._retry_delay_seconds(bare, attempt=10) == 30.0
