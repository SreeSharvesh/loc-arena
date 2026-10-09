"""Tests for OpenRouter provider routing and reasoning settings."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx2
import pytest
from loc_arena.config import (
    ConfigError,
    ModelSpec,
    ProviderPreferences,
    ReasoningPreferences,
    load_run_config,
)
from loc_arena.gateway import core
from loc_arena.gateway.core import GatewayCore, GenerateRequest, OpenRouterProvider
from loc_arena.logging_.events import AppendOnlyLog

MESSAGES = [{"role": "user", "content": "hello"}]
TIMEOUT_SECONDS = 5.0  # the posts are stubbed: no call waits
PINNED_MODEL = "deepseek/deepseek-v4.1-flash"
UNLISTED_MODEL = "meta-llama/llama-3.1-8b-instruct"
MODEL_PROVIDERS = {
    PINNED_MODEL: ProviderPreferences(order=["together", "baseten", "deepseek"], allow_fallbacks=False),
}


def _capture_post(
    monkeypatch: pytest.MonkeyPatch,
    content: str = "ok",
) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def fake_post(*_args: object, json: dict[str, Any], **_kwargs: object) -> httpx2.Response:
        bodies.append(json)
        return httpx2.Response(
            status_code=200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
            request=httpx2.Request("POST", core.OPENROUTER_URL),
        )

    monkeypatch.setattr("loc_arena.gateway.core.httpx2.post", fake_post)
    return bodies


def test_a_call_carries_its_models_providers_and_its_roles_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture_post(monkeypatch)
    provider = OpenRouterProvider(
        api_key="test-key",
        timeout=TIMEOUT_SECONDS,
        model_providers=MODEL_PROVIDERS,
    )
    spec = ModelSpec(
        model=PINNED_MODEL,
        temperature=1.0,
        max_tokens=8192,
        reasoning=ReasoningPreferences(effort="high"),
    )

    provider.generate(PINNED_MODEL, MESSAGES, 1.0, 8192, None, spec=spec)

    assert bodies == [
        {
            "model": PINNED_MODEL,
            "messages": MESSAGES,
            "temperature": 1.0,
            "max_tokens": 8192,
            "provider": {"order": ["together", "baseten", "deepseek"], "allow_fallbacks": False},
            "reasoning": {"effort": "high"},
        },
    ]


def test_a_call_to_a_model_with_no_providers_listed_is_left_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _capture_post(monkeypatch)
    provider = OpenRouterProvider(
        api_key="test-key",
        timeout=TIMEOUT_SECONDS,
        model_providers=MODEL_PROVIDERS,
    )
    spec = ModelSpec(model=UNLISTED_MODEL, temperature=0.0, max_tokens=4096)

    provider.generate(UNLISTED_MODEL, MESSAGES, 0.0, 4096, None, spec=spec)

    assert bodies == [{"model": UNLISTED_MODEL, "messages": MESSAGES, "temperature": 0.0, "max_tokens": 4096}]


def test_models_file_rejects_unknown_field_under_provider_naming_it(tmp_path: Path) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    models_file = tmp_path / "configs" / "models.custom.yaml"
    models_file.write_text(
        "providers:\n"
        "  deepseek/deepseek-v4.1-flash: {order: [together], bogus_routing_field: true}\n"
        "roles:\n"
        "  untrusted_agent:\n"
        "    model: deepseek/deepseek-v4.1-flash\n"
        "    temperature: 1.0\n"
        "    max_tokens: 8192\n",
    )
    run_file = tmp_path / "configs" / "run.yaml"
    run_file.write_text("extends: aurora-efficiency.deterministic.yaml\nmodels: models.custom.yaml\n")

    with pytest.raises(ConfigError) as exc_info:
        load_run_config(run_file)

    assert "bogus_routing_field" in str(exc_info.value)


def test_models_file_rejects_unknown_field_under_reasoning_naming_it(tmp_path: Path) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    models_file = tmp_path / "configs" / "models.custom.yaml"
    models_file.write_text(
        "roles:\n"
        "  untrusted_agent:\n"
        "    model: deepseek/deepseek-v4.1-flash\n"
        "    temperature: 1.0\n"
        "    max_tokens: 8192\n"
        "    reasoning:\n"
        "      effort: high\n"
        "      bogus_reasoning_field: 42\n",
    )
    run_file = tmp_path / "configs" / "run.yaml"
    run_file.write_text("extends: aurora-efficiency.deterministic.yaml\nmodels: models.custom.yaml\n")

    with pytest.raises(ConfigError) as exc_info:
        load_run_config(run_file)

    assert "bogus_reasoning_field" in str(exc_info.value)


def test_roles_with_same_model_id_send_distinct_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _capture_post(monkeypatch)
    provider = OpenRouterProvider(api_key="test-key", timeout=TIMEOUT_SECONDS)
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    core = GatewayCore(
        config,
        "ep-1",
        AppendOnlyLog(tmp_path / "sealed.jsonl", "ep-1"),
        provider,
        turn_secret="secret",
    )
    requests = (
        GenerateRequest(prompt="agent call", caller_identity="agent", role="untrusted_agent"),
        GenerateRequest(prompt="teacher call", caller_identity="teacher", role="teacher"),
    )

    for request in requests:
        core.generate(request)

    assert [body.get("reasoning") for body in bodies] == [None, {"effort": "none"}]


def test_fast_run_disables_agent_reasoning_while_inheriting_other_roles() -> None:
    base = load_run_config("configs/aurora-efficiency.yaml")

    fast = load_run_config("configs/aurora-efficiency.fast.yaml")

    assert (
        fast.models["untrusted_agent"].reasoning,
        {role: spec for role, spec in fast.models.items() if role != "untrusted_agent"},
        fast.model_providers,
    ) == (
        ReasoningPreferences(effort="none"),
        {role: spec for role, spec in base.models.items() if role != "untrusted_agent"},
        base.model_providers,
    )
