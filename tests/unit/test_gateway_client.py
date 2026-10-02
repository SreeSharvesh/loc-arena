"""The runner's model clients over the edge's real app: ``EdgeClient`` and each agent's ``GatewayClient``."""

from __future__ import annotations

from fastapi.testclient import TestClient
from loc_arena.gateway.client import EdgeClient, GatewayClient
from loc_arena.gateway.core_control_client import CoreControlClient
from loc_arena.stack.contracts import GenerateRequest
from loc_arena.stack.service_client import ServiceClient

from tests.unit._gateway_stack import GatewayStack


def _edge(edge: TestClient) -> EdgeClient:
    return EdgeClient(ServiceClient(edge))


def test_the_edge_client_returns_the_mirror_seq_the_edge_wrote(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    reply = _edge(stack.edge).generate(GenerateRequest(prompt="p", caller_identity="coworker"))

    assert stack.mirror_events(control.handle)[0].seq == reply.mirror_seq


def test_an_agents_call_inside_a_sanctioned_turn_carries_the_turn_id(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    client = GatewayClient(_edge(stack.edge), "serving-agent")
    client.set_turn_token(control.mint_turn_token("serving-agent", 2))

    client.generate("plan", role="untrusted_agent")

    assert stack.sealed_events(control.handle)[0].payload["turn_id"] == "serving-agent:t2"


def test_an_agents_call_after_its_turn_is_off_path(stack: GatewayStack, control: CoreControlClient) -> None:
    client = GatewayClient(_edge(stack.edge), "serving-agent")
    client.set_turn_token(control.mint_turn_token("serving-agent", 2))
    client.set_turn_token(None)

    client.generate("plan", role="untrusted_agent")

    assert stack.sealed_events(control.handle)[0].payload["credential_kind"] == "none"


def test_a_batch_returns_one_completion_per_prompt(stack: GatewayStack, control: CoreControlClient) -> None:
    client = GatewayClient(_edge(stack.edge), "agent-main/distill-agent")

    texts = client.batch_generate(["shard 1", "shard 2"])

    assert texts == ["gen#1", "gen#2"]
