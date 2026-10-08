"""A provider pointed at the gateway (``LOC_ARENA_GATEWAY_URL``) sends its calls there and holds no real key.

``httpx.post`` is stubbed: it is the paid provider boundary.
"""

from __future__ import annotations

import httpx
import pytest
from loc_arena.gateway import core

GATEWAY = "http://gateway:8080/api/v1/chat/completions"
COMPLETION = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}
MESSAGES: list[core.Message] = [{"role": "user", "content": "hi"}]


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def record_post(url: str, *, headers: dict[str, str], **_options: object) -> httpx.Response:
        sent.append(httpx.Request("POST", url, headers=headers))
        return httpx.Response(200, json=COMPLETION, request=sent[-1])

    monkeypatch.setattr("loc_arena.gateway.core.httpx.post", record_post)
    return sent


def test_a_provider_behind_the_gateway_posts_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
    posted: list[httpx.Request],
) -> None:
    monkeypatch.delenv(core.API_KEY_VARIABLE, raising=False)
    monkeypatch.setenv(core.GATEWAY_URL_VARIABLE, GATEWAY)

    core.OpenRouterProvider().generate("m", MESSAGES, 0.0, 8, None)

    assert str(posted[0].url) == GATEWAY


def test_a_provider_behind_the_gateway_withholds_a_real_key_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
    posted: list[httpx.Request],
) -> None:
    monkeypatch.setenv(core.GATEWAY_URL_VARIABLE, GATEWAY)

    core.OpenRouterProvider(api_key="sk-real").generate("m", MESSAGES, 0.0, 8, None)

    assert "sk-real" not in posted[0].headers["authorization"]


def test_a_gateway_url_alone_configures_a_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(core.API_KEY_VARIABLE, raising=False)
    monkeypatch.setenv(core.GATEWAY_URL_VARIABLE, GATEWAY)

    configured = core.is_live_provider_configured()

    assert configured


def test_without_a_gateway_or_a_key_no_live_provider_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(core.API_KEY_VARIABLE, raising=False)
    monkeypatch.delenv(core.GATEWAY_URL_VARIABLE, raising=False)

    configured = core.is_live_provider_configured()

    assert not configured
