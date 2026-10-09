"""A provider pointed at the gateway (``LOC_ARENA_GATEWAY_URL``) sends its calls there and holds no real key.

``httpx2.post`` is stubbed: it is the paid provider boundary.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import httpx2
import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway import core
from loc_arena.live import play_model_episode

TIMEOUT_SECONDS = 1.0  # stubbed posts never wait

GATEWAY = "http://gateway:8080/api/v1/chat/completions"
COMPLETION = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}
MESSAGES: list[core.Message] = [{"role": "user", "content": "hi"}]
GATEWAY_TIMEOUT_SECONDS = 120  # gateway.timeout_seconds in configs/env.default.yaml


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    sent: list[httpx2.Request] = []

    def record_post(url: str, *, headers: dict[str, str], **_options: object) -> httpx2.Response:
        sent.append(httpx2.Request("POST", url, headers=headers))
        return httpx2.Response(200, json=COMPLETION, request=sent[-1])

    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", record_post)
    return sent


def test_a_provider_behind_the_gateway_posts_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
    posted: list[httpx2.Request],
) -> None:
    monkeypatch.delenv(core.API_KEY_VARIABLE, raising=False)
    monkeypatch.setenv(core.GATEWAY_URL_VARIABLE, GATEWAY)

    core.OpenRouterProvider(timeout=TIMEOUT_SECONDS).generate("m", MESSAGES, 0.0, 8, None)

    assert str(posted[0].url) == GATEWAY


def test_a_provider_behind_the_gateway_withholds_a_real_key_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
    posted: list[httpx2.Request],
) -> None:
    monkeypatch.setenv(core.GATEWAY_URL_VARIABLE, GATEWAY)

    core.OpenRouterProvider(api_key="sk-real", timeout=TIMEOUT_SECONDS).generate("m", MESSAGES, 0.0, 8, None)

    assert "sk-real" not in posted[0].headers["authorization"]


def test_a_provider_given_a_gateway_url_posts_there_despite_a_key_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    posted: list[httpx2.Request],
) -> None:
    monkeypatch.setenv(core.API_KEY_VARIABLE, "sk-real")
    monkeypatch.delenv(core.GATEWAY_URL_VARIABLE, raising=False)

    core.OpenRouterProvider(gateway_url=GATEWAY, timeout=TIMEOUT_SECONDS).generate(
        "m",
        MESSAGES,
        0.0,
        8,
        None,
    )

    assert str(posted[0].url) == GATEWAY


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


def test_subprocesses_running_agent_code_do_not_inherit_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(core.API_KEY_VARIABLE, "sk-real")

    environment = core.environment_without_key()

    assert core.API_KEY_VARIABLE not in environment


def test_a_live_episode_waits_for_a_reply_as_long_as_the_gateway_does(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    timeouts: set[float] = set()

    def record_post(url: str, *, timeout: float, **_options: object) -> httpx2.Response:
        timeouts.add(timeout)
        return httpx2.Response(200, json=COMPLETION, request=httpx2.Request("POST", url))

    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", record_post)
    monkeypatch.setenv(core.GATEWAY_URL_VARIABLE, GATEWAY)
    config = dataclasses.replace(
        load_run_config("configs/aurora-efficiency.deterministic.yaml"),
        policy="model",
    )

    play_model_episode(config, tmp_path)

    assert timeouts == {GATEWAY_TIMEOUT_SECONDS}
