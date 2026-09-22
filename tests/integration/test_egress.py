from __future__ import annotations

from loc_arena.harness import EpisodeStack

from tests.integration._docker_support import can_egress, service_networks


def test_gateway_core_is_the_only_egress(stack: EpisodeStack) -> None:
    services = (
        "gateway_core",
        "gateway_edge",
        "execution",
        "cluster",
        "forge",
        "weights",
        "siem",
        "ticketboard",
        "wiki",
        "recorder",
    )
    on_egress = {s for s in services if "egress-net" in service_networks(stack, s)}
    assert on_egress == {"gateway_core"}  # exactly one service holds the egress (structural)
    assert "egress-net" not in service_networks(stack, "gateway_edge")  # the edge holds no egress


def test_core_can_reach_the_provider(stack: EpisodeStack) -> None:
    # the egress direction is reliable: the one service on egress-net reaches the internet
    assert can_egress(stack, "gateway_core") is True
