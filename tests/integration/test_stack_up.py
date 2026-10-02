from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from loc_arena.compose_document import render_compose
from loc_arena.compose_stack import docker_available, teardown, up
from loc_arena.config import load_run_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable"),
]

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
# profiled services (the runner, the grader) are on-demand: `docker compose run`, not `up`
EXPECTED = {
    name for name, service in render_compose(CONFIG)["services"].items() if not service.get("profiles")
}


def test_up_brings_every_service_up_healthy(tmp_path: Path) -> None:
    # up(--wait) fails when any service does not become healthy
    stack = up(CONFIG, project=f"locarena-stackup-{secrets.token_hex(3)}", workdir=tmp_path)
    try:
        running = stack.running_services()
    finally:
        teardown(stack)

    assert running == EXPECTED


def test_teardown_leaves_no_container_of_the_project(tmp_path: Path) -> None:
    stack = up(CONFIG, project=f"locarena-stackup-{secrets.token_hex(3)}", workdir=tmp_path)

    teardown(stack)

    assert stack.running_services() == set()
