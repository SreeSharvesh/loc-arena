from __future__ import annotations

import pytest
from loc_arena.compose_stack import EpisodeStack

from tests.integration._docker_support import can_egress, service_networks

pytestmark = pytest.mark.integration  # every test here brings the stack up


def test_gateway_core_is_the_only_service_on_the_egress_network(stack: EpisodeStack) -> None:
    # the core, the recorder, the edge, every sandbox and the evidence reader
    services = stack.running_services()

    on_egress = {service for service in services if "egress-net" in service_networks(stack, service)}

    assert on_egress == {"gateway_core"}  # exactly one service holds the egress (structural)


def test_core_can_reach_the_provider(stack: EpisodeStack) -> None:
    # the egress direction is reliable: the one service on egress-net reaches the internet
    reachable = can_egress(stack, "gateway_core")

    assert reachable is True
