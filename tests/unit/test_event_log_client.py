"""The runner's sealed and mirror ``EventLog`` clients, against the core's and the edge's real routes."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from loc_arena.gateway.core_control_client import CoreControlClient
from loc_arena.gateway.event_log_client import MirrorEventLog, SealedEventLog
from loc_arena.logging_.events import EventDraft
from loc_arena.stack.service_client import ServiceClient

from tests.unit._gateway_stack import KEY, OPENING, GatewayStack, serve_gateway

DRAFT = EventDraft(
    ts=120.0,
    actor_uid="agent-main",
    actor_role="orchestrator",
    kind="message",
    payload={"b": 1},
)


@pytest.fixture
def stack(tmp_path: Path) -> GatewayStack:
    return serve_gateway(tmp_path)


@pytest.fixture
def control(stack: GatewayStack) -> CoreControlClient:
    return CoreControlClient.open_episode(stack.control, OPENING)


def test_a_sealed_event_recorded_through_the_core_is_written_by_the_recorder(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    sealed = SealedEventLog(stack.control, control.handle)

    written = sealed.record(DRAFT)

    assert stack.sealed_events(control.handle) == [written]


def test_the_sealed_client_knows_the_seq_the_core_gave_its_event(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    control.call_model_as_monitor("action_monitor", "a call the core records first", 0.0)
    sealed = SealedEventLog(stack.control, control.handle)

    sealed.record(DRAFT)

    assert sealed.last_seq == 1


def test_a_mirror_event_recorded_through_the_edge_is_written_to_the_mirror(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    mirror = MirrorEventLog(ServiceClient(stack.edge, control_key=KEY), control.handle, control.episode_id)

    written = mirror.record(DRAFT)

    assert stack.mirror_events(control.handle) == [written]


def test_the_mirror_client_without_the_control_key_is_refused(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    mirror = MirrorEventLog(ServiceClient(stack.edge), control.handle, control.episode_id)

    with pytest.raises(httpx.HTTPStatusError) as refusal:
        mirror.record(DRAFT)

    assert refusal.value.response.status_code == HTTPStatus.UNAUTHORIZED
