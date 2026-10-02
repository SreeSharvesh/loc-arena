"""Shared fixtures for the integration tests: a session-scoped docker stack, and the live model provider."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Final

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.openrouter_provider import OpenRouterProvider, live_provider_from_environment
from loc_arena.harness import DOTENV_PATH, EpisodeStack, docker_available, teardown, up

LIVE_SWITCH: Final = "LOC_ARENA_LIVE"


@pytest.fixture
def live_provider() -> OpenRouterProvider:
    if os.environ.get(LIVE_SWITCH) != "1":
        pytest.skip(f"live model calls are off (set {LIVE_SWITCH}=1)")
    settings = load_run_config("configs/aurora-efficiency.deterministic.yaml").settings.provider
    provider = live_provider_from_environment(settings, dotenv_path=DOTENV_PATH)
    if provider is None:
        pytest.skip("no OpenRouter key in the environment or .env")
    return provider


@pytest.fixture(scope="session")
def stack() -> Iterator[EpisodeStack]:
    """Bring the per-episode stack up once for the isolation tests; tear it down at the end."""
    if not docker_available():
        pytest.skip("docker daemon unavailable")
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    s = up(config, project="locarena-p3")
    try:
        yield s
    finally:
        teardown(s)
