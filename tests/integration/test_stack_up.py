from __future__ import annotations

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import docker_available, teardown, up

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")


def test_up_brings_all_services_healthy_and_teardown_is_clean() -> None:
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    expected = set(config.raw["services"])
    stack = up(config, project="locarena-stackup")  # up(--wait) fails if any service is unhealthy
    try:
        assert stack.running_services() == expected  # every configured service is up
    finally:
        teardown(stack)
    assert stack.running_services() == set()  # teardown left zero of THIS project's containers
