"""Shared fixtures for the integration tests (a session-scoped docker stack for the isolation checks)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import EpisodeStack, docker_available, teardown, up


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
