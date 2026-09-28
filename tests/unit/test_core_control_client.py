"""The runner's ``CoreControlClient`` against the core's real control routes (FastAPI ``TestClient``)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from loc_arena.gateway.core import DeterministicProvider
from loc_arena.gateway.core_control_client import CoreControlClient, read_core_health
from loc_arena.stack.constants import GENERATE_ROUTE
from loc_arena.stack.contracts import CoreGenerateResponse, GenerateRequest
from loc_arena.stack.service_client import ServiceClient

from tests.unit._gateway_stack import OPENING, GatewayStack, serve_gateway


@pytest.fixture
def stack(tmp_path: Path) -> GatewayStack:
    return serve_gateway(tmp_path)


@pytest.fixture
def control(stack: GatewayStack) -> CoreControlClient:
    return CoreControlClient.open_episode(stack.control, OPENING)


def _generate(stack: GatewayStack, request: GenerateRequest) -> CoreGenerateResponse:
    return ServiceClient(stack.core).post_model(GENERATE_ROUTE, request, CoreGenerateResponse)


def _durable_call(credential: str) -> GenerateRequest:
    return GenerateRequest(
        prompt="loop",
        caller_identity="agent-main/distill-agent",
        durable_credential=credential,
    )


def test_an_opened_episode_carries_the_id_built_from_the_run_name_and_mode(stack: GatewayStack) -> None:
    control = CoreControlClient.open_episode(stack.control, OPENING)

    assert control.episode_id == f"{OPENING.run_config_name}-{OPENING.mode}"


def test_the_clock_the_client_sets_stamps_the_cores_records(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    control.set_clock(4321.0)

    reply = _generate(stack, GenerateRequest(prompt="p", caller_identity="coworker"))

    assert stack.sealed_events(control.handle)[reply.seq].ts == 4321.0


def test_a_turn_token_the_client_mints_makes_the_call_sanctioned(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    token = control.mint_turn_token("serving-agent", 3)

    reply = _generate(stack, GenerateRequest(prompt="p", caller_identity="serving-agent", turn_token=token))

    assert reply.turn_id == "serving-agent:t3"


def test_logging_coverage_the_client_sets_keeps_an_uncovered_caller_out_of_the_mirror(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    control.set_coverage("logging", frozenset({"agent-main"}))

    reply = _generate(stack, GenerateRequest(prompt="p", caller_identity="batch-runner"))

    assert reply.in_mirror is False


def test_a_durable_credential_the_client_issues_resolves_to_its_account(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    credential = control.issue_durable_credential("batch-runner", sanctioned=False)

    reply = _generate(stack, _durable_call(credential.token.get_secret_value()))

    assert (reply.credential_kind, reply.caller_identity) == ("durable_identity", "batch-runner")


def test_a_rotation_through_the_client_revokes_the_old_instance(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    old = control.issue_durable_credential("batch-runner", sanctioned=False)
    control.rotate_durable_credential("batch-runner", sanctioned=False)

    reply = _generate(stack, _durable_call(old.token.get_secret_value()))

    assert reply.credential_kind == "none"


def test_a_rotation_reports_the_instance_it_revoked(control: CoreControlClient) -> None:
    old = control.issue_durable_credential("batch-runner", sanctioned=False)

    rotation = control.rotate_durable_credential("batch-runner", sanctioned=False)

    assert (rotation.revoked_instance, rotation.credential.instance) == (old.instance, "batch-runner-i2")


def test_a_monitor_call_through_the_client_is_recorded_under_a_monitor_credential(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    control.call_model_as_monitor("action_monitor", "score this", 0.0)

    (record,) = stack.sealed_events(control.handle)
    assert (record.payload["credential_kind"], record.actor_uid) == ("monitor", "action_monitor")


def test_closing_returns_the_last_seq_of_the_sealed_log(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    _generate(stack, GenerateRequest(prompt="a", caller_identity="coworker"))
    _generate(stack, GenerateRequest(prompt="b", caller_identity="coworker"))

    last_sealed_seq = control.close()

    assert last_sealed_seq == 1


def test_a_closed_episode_no_longer_serves_model_calls(
    stack: GatewayStack,
    control: CoreControlClient,
) -> None:
    control.close()

    status = stack.core.post(GENERATE_ROUTE, json={"prompt": "late", "caller_identity": "x"}).status_code

    assert status == HTTPStatus.CONFLICT


def test_a_client_without_the_control_key_is_refused(stack: GatewayStack, control: CoreControlClient) -> None:
    keyless = CoreControlClient(ServiceClient(stack.core), control.handle, control.episode_id)

    with pytest.raises(httpx.HTTPStatusError) as refusal:
        keyless.set_clock(1.0)

    assert refusal.value.response.status_code == HTTPStatus.UNAUTHORIZED


def test_the_health_route_says_whether_the_core_holds_a_provider_key(tmp_path: Path) -> None:
    keyed = serve_gateway(tmp_path, provider=DeterministicProvider())

    health = read_core_health(keyed.control)

    assert health.provider_configured is True
