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


def test_posts_provider_and_reasoning_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture_post(monkeypatch)
    provider = OpenRouterProvider(api_key="test-key")
    spec = ModelSpec(
        model="deepseek/deepseek-v4.1-flash",
        temperature=1.0,
        max_tokens=8192,
        provider=ProviderPreferences(
            order=["together", "baseten", "deepseek"],
            allow_fallbacks=False,
        ),
        reasoning=ReasoningPreferences(enabled=True),
    )

    provider.generate("deepseek/deepseek-v4.1-flash", MESSAGES, 1.0, 8192, None, spec=spec)

    assert bodies == [
        {
            "model": "deepseek/deepseek-v4.1-flash",
            "messages": MESSAGES,
            "temperature": 1.0,
            "max_tokens": 8192,
            "provider": {
                "order": ["together", "baseten", "deepseek"],
                "allow_fallbacks": False,
            },
            "reasoning": {"enabled": True},
        },
    ]


def test_omits_provider_and_reasoning_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture_post(monkeypatch)
    provider = OpenRouterProvider(api_key="test-key")
    spec = ModelSpec(
        model="meta-llama/llama-3.1-8b-instruct",
        temperature=0.0,
        max_tokens=4096,
    )

    provider.generate("meta-llama/llama-3.1-8b-instruct", MESSAGES, 0.0, 4096, None, spec=spec)

    assert "provider" not in bodies[0]
    assert "reasoning" not in bodies[0]


def test_models_file_rejects_unknown_field_under_provider_naming_it(tmp_path: Path) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    models_file = tmp_path / "configs" / "models.custom.yaml"
    models_file.write_text(
        "roles:\n"
        "  untrusted_agent:\n"
        "    model: deepseek/deepseek-v4.1-flash\n"
        "    temperature: 1.0\n"
        "    max_tokens: 8192\n"
        "    provider:\n"
        "      order: [together]\n"
        "      bogus_routing_field: true\n",
    )
    run_file = tmp_path / "configs" / "run.yaml"
    run_file.write_text("extends: aurora-efficiency.deterministic.yaml\nmodels: models.custom.yaml\n")

    with pytest.raises(ConfigError, match=r"bogus_routing_field"):
        load_run_config(run_file)


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
        "      enabled: true\n"
        "      bogus_reasoning_field: 42\n",
    )
    run_file = tmp_path / "configs" / "run.yaml"
    run_file.write_text("extends: aurora-efficiency.deterministic.yaml\nmodels: models.custom.yaml\n")

    with pytest.raises(ConfigError, match=r"bogus_reasoning_field"):
        load_run_config(run_file)


def test_roles_with_same_model_id_send_distinct_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _capture_post(monkeypatch)
    provider = OpenRouterProvider(api_key="test-key")
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    core = GatewayCore(
        config,
        "ep-1",
        AppendOnlyLog(tmp_path / "sealed.jsonl", "ep-1"),
        provider,
        turn_secret="secret",
    )

    core.generate(GenerateRequest(prompt="agent call", caller_identity="agent", role="untrusted_agent"))
    core.generate(GenerateRequest(prompt="teacher call", caller_identity="teacher", role="teacher"))

    assert len(bodies) == 2
    assert bodies[0]["model"] == bodies[1]["model"]
    assert bodies[0]["reasoning"] == {"enabled": True}
    assert bodies[1]["reasoning"] == {"enabled": False}
