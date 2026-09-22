from __future__ import annotations

from loc_arena.harness import EpisodeStack

from tests.integration._docker_support import path_exists, service_networks, share_a_network

# The load-bearing check (the sealed-vs-tamperable isolation): the agent sandbox is structurally isolated
# from the sealed
# layer and the provider, enforced at the network and volume layer -- NOT in application code. Asserted by
# network membership and volume mounts (deterministic) plus the mount-based probe on the sealed volume.


def test_execution_shares_no_network_with_the_sealed_layer(stack: EpisodeStack) -> None:
    # the agent sandbox is on agent-net ONLY: not on control-net (the core), sealed-net (the recorder), or
    # egress-net (the provider), so it has no route to any of them
    assert service_networks(stack, "execution") == {"agent-net"}
    assert share_a_network(stack, "execution", "gateway_core") is False  # cannot reach the sealed core
    assert share_a_network(stack, "execution", "recorder") is False  # cannot reach the sealed recorder
    # the sanctioned path exists: execution shares agent-net with the edge
    assert share_a_network(stack, "execution", "gateway_edge") is True


def test_execution_cannot_read_or_write_the_sealed_volume(stack: EpisodeStack) -> None:
    # the sealed log volume is not mounted into execution at all (a mount fact; deterministic)
    assert path_exists(stack, "execution", "/sealed/events.jsonl") is False
    write = stack.exec(
        "execution",
        ["sh", "-c", "echo x > /sealed/events.jsonl 2>&1 || echo WRITE_DENIED"],
        check=False,
    )
    assert "WRITE_DENIED" in write.stdout
