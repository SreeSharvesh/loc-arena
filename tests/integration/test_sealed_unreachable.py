from __future__ import annotations

import pytest
from loc_arena.compose_stack import EpisodeStack
from loc_arena.config import load_run_config
from loc_arena.stack.constants import build_sandbox_service_name

from tests.integration._docker_support import has_mount_at, service_networks, share_a_network

pytestmark = pytest.mark.integration  # every test here brings the stack up

# Every agent's sandbox is cut off from the sealed layer and the provider by its networks and mounts.
SANDBOXES = [
    build_sandbox_service_name(agent.id)
    for agent in load_run_config("configs/aurora-efficiency.deterministic.yaml").agents
]


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_a_sandbox_is_on_the_agent_network_only(stack: EpisodeStack, sandbox: str) -> None:
    # agent-net ONLY: not control-net (the core), sealed-net (the recorder), or egress-net (the provider)
    networks = service_networks(stack, sandbox)

    assert networks == {"agent-net"}


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_a_sandbox_shares_no_network_with_the_core_or_the_recorder(stack: EpisodeStack, sandbox: str) -> None:
    shared = {service for service in ("gateway_core", "recorder") if share_a_network(stack, sandbox, service)}

    assert shared == set()


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_a_sandbox_shares_the_agent_network_with_the_edge(stack: EpisodeStack, sandbox: str) -> None:
    # the sanctioned path exists: the sandbox reaches a model only through the edge
    shared = share_a_network(stack, sandbox, "gateway_edge")

    assert shared is True


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_a_sandbox_has_no_sealed_volume(stack: EpisodeStack, sandbox: str) -> None:
    # the sealed log volume is not mounted into any sandbox at all (a mount fact; deterministic)
    mounted = has_mount_at(stack, sandbox, "/sealed")

    assert mounted is False
