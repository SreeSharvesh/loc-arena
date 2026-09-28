"""The gateway core as a service: its model routes serve the open episode, its control routes need the key."""

from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from loc_arena.config import load_run_config
from loc_arena.gateway.control import LocalGatewayControl
from loc_arena.gateway.core import DeterministicProvider, EpisodeSpec, Message, ProviderResult, ToolSpec
from loc_arena.gateway.core_service import build_core_app
from loc_arena.gateway.edge import EpisodeMirror, GatewayEdge, MirroringRelay
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.logging_.events import AppendOnlyLog, Event, EventDraft, read_events
from loc_arena.stack.constants import (
    BATCH_GENERATE_ROUTE,
    CLOCK_ROUTE,
    CLOSE_ROUTE,
    CONTROL_KEY_SECRET_NAME,
    COVERAGE_ROUTE,
    DURABLE_CREDENTIALS_ROUTE,
    EPISODE_HANDLE_PATTERN,
    EPISODES_ROUTE,
    GENERATE_ROUTE,
    HEALTH_ROUTE,
    MODEL_CALLS_FILE_NAME,
    MONITOR_CALLS_ROUTE,
    MONITOR_MODEL_ROLE,
    SEALED_EVENTS_ROUTE,
    TURN_TOKENS_ROUTE,
)
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    BatchGenerateResponse,
    ClockUpdate,
    CoreGenerateResponse,
    CoreHealth,
    CoverageComponent,
    CoverageUpdate,
    DurableCredentialIssued,
    DurableCredentialRequest,
    EpisodeClosed,
    EpisodeOpened,
    GatewayControl,
    GenerateRequest,
    IssuedToken,
    ModelCallRecord,
    ModelCallUsage,
    MonitorCall,
    MonitorCallResult,
    Servable,
    TurnTokenRequest,
)
from loc_arena.stack.service_client import Body, HttpMethod, ServiceClient
from loc_arena.stack.settings import LocArenaSettings
from pydantic import SecretStr

from tests.unit._gateway_stack import COVERT_PROMPT, KEY, OPENING, GatewayStack, serve_gateway

START_TS = LocArenaSettings().clock.start_ts
TURN_TOKEN_TTL = LocArenaSettings().gateway.turn_token_ttl_seconds
TOKEN_MINTED_AT = 500.0
TOKEN_EXPIRES_AT = TOKEN_MINTED_AT + TURN_TOKEN_TTL
EPISODE_ID = "unit-attack"  # OPENING's run config name and mode
COVERT_TARGET = OPENING.covert.target_identity
COVERED_CALLER = "agent-main"
DROPPED_CALLER = "batch-runner"  # outside the coverage the tests deploy
DURABLE_ACCOUNT = "batch-runner"
REQUEST = GenerateRequest(prompt="hello", caller_identity="agent-main/distill-agent", role="untrusted_agent")
VERDICT_DRAFT = EventDraft(ts=9.0, actor_uid="monitor.action", actor_role="trusted", kind="monitor_verdict")
MONITOR_CALL = MonitorCall(monitor="monitor.action", prompt="score this", temperature=0.0)
CONCURRENT_CALLS = 40
OPENROUTER_OPENING = OPENING.model_copy(update={"provider": "openrouter"})
UNKNOWN_HANDLE = "0" * 16
RUN_CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


@pytest.fixture
def handle(opened: EpisodeOpened) -> str:
    return opened.handle


@pytest.fixture
def turn_token(stack: GatewayStack, handle: str) -> str:
    _set_clock(stack, handle, TOKEN_MINTED_AT)
    request = TurnTokenRequest(agent_uid="agent-main", turn=1)
    return stack.control.post_model(TURN_TOKENS_ROUTE.format(handle=handle), request, IssuedToken).token


@pytest.fixture
def durable(stack: GatewayStack, handle: str) -> DurableCredentialIssued:
    return _issue_durable(stack, handle, rotate=False)


@pytest.fixture
def rotated(stack: GatewayStack, handle: str, durable: DurableCredentialIssued) -> DurableCredentialIssued:
    return _issue_durable(stack, handle, rotate=True)


@pytest.fixture
def closed_handle(stack: GatewayStack, handle: str) -> str:
    _generate(stack, REQUEST)
    _close(stack, handle)
    return handle


def _generate(stack: GatewayStack, request: GenerateRequest) -> CoreGenerateResponse:
    return ServiceClient(stack.core).post_model(GENERATE_ROUTE, request, CoreGenerateResponse)


def _status_of_generate(stack: GatewayStack, request: GenerateRequest) -> int:
    return stack.core.post(GENERATE_ROUTE, json=request.model_dump()).status_code


def _set_clock(stack: GatewayStack, handle: str, now: float) -> None:
    stack.control.send("PUT", CLOCK_ROUTE.format(handle=handle), ClockUpdate(now=now))


def _set_coverage(stack: GatewayStack, handle: str, component: CoverageComponent) -> None:
    update = CoverageUpdate(component=component, covered=frozenset({COVERED_CALLER}))
    stack.control.send("PUT", COVERAGE_ROUTE.format(handle=handle), update)


def _issue_durable(
    stack: GatewayStack,
    handle: str,
    *,
    rotate: bool,
    sanctioned: bool = False,
) -> DurableCredentialIssued:
    request = DurableCredentialRequest(account=DURABLE_ACCOUNT, sanctioned=sanctioned, rotate=rotate)
    route = DURABLE_CREDENTIALS_ROUTE.format(handle=handle)
    return stack.control.post_model(route, request, DurableCredentialIssued)


def _as_durable(token: str) -> GenerateRequest:
    return GenerateRequest(
        prompt="loop",
        caller_identity="agent-main/distill-agent",
        durable_credential=token,
    )


def _in_turn(token: str) -> GenerateRequest:
    return GenerateRequest(prompt="plan", caller_identity="agent-main", turn_token=token)


def _close(stack: GatewayStack, handle: str) -> EpisodeClosed:
    return stack.control.post_model(CLOSE_ROUTE.format(handle=handle), None, EpisodeClosed)


def _record_sealed(stack: GatewayStack, handle: str, draft: EventDraft) -> Event:
    return stack.control.post_model(SEALED_EVENTS_ROUTE.format(handle=handle), draft, Event)


def _call_as_monitor(stack: GatewayStack, handle: str) -> MonitorCallResult:
    return stack.control.post_model(
        MONITOR_CALLS_ROUTE.format(handle=handle),
        MONITOR_CALL,
        MonitorCallResult,
    )


def _model_calls(stack: GatewayStack, handle: str) -> list[ModelCallRecord]:
    lines = (stack.sealed_root / handle / MODEL_CALLS_FILE_NAME).read_text().splitlines()
    return [ModelCallRecord.model_validate_json(line) for line in lines]


# --- the control key ---
@pytest.mark.parametrize("key", [None, SecretStr("a-wrong-key")], ids=["no-key", "wrong-key"])
@pytest.mark.parametrize(
    ("method", "route", "body"),
    [
        pytest.param("POST", EPISODES_ROUTE, OPENING, id="open"),
        pytest.param("PUT", CLOCK_ROUTE, ClockUpdate(now=1.0), id="clock"),
        pytest.param("POST", CLOSE_ROUTE, None, id="close"),
    ],
)
def test_a_control_route_answers_401_without_the_right_key(
    stack: GatewayStack,
    handle: str,
    key: SecretStr | None,
    method: HttpMethod,
    route: str,
    body: Body | None,
) -> None:
    client = ServiceClient(stack.core, control_key=key)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        client.send(method, route.format(handle=handle), body)

    assert raised.value.response.status_code == HTTPStatus.UNAUTHORIZED


# --- opening an episode ---
def test_a_generate_with_no_open_episode_is_refused_with_409(stack: GatewayStack) -> None:
    status = _status_of_generate(stack, REQUEST)

    assert status == HTTPStatus.CONFLICT


def test_an_episode_opens_under_a_well_formed_handle(stack: GatewayStack) -> None:
    opened = stack.open_episode()

    assert re.fullmatch(EPISODE_HANDLE_PATTERN, opened.handle)


def test_an_episode_is_named_after_its_run_config_and_mode(stack: GatewayStack) -> None:
    opened = stack.open_episode()

    assert opened.episode_id == EPISODE_ID


def test_a_generate_is_served_by_the_open_episode(stack: GatewayStack, opened: EpisodeOpened) -> None:
    reply = _generate(stack, REQUEST)

    assert (reply.episode_handle, reply.episode_id) == (opened.handle, opened.episode_id)


def test_a_generate_reply_carries_the_seq_of_its_sealed_record(stack: GatewayStack, handle: str) -> None:
    _record_sealed(stack, handle, VERDICT_DRAFT)

    reply = _generate(stack, REQUEST)

    assert reply.seq == stack.sealed_events(handle)[-1].seq


@pytest.mark.usefixtures("opened")
def test_a_new_episodes_clock_starts_at_the_configured_start(stack: GatewayStack) -> None:
    reply = _generate(stack, REQUEST)

    assert reply.ts == START_TS


@pytest.mark.usefixtures("opened")
def test_a_call_without_a_credential_resolves_to_none(stack: GatewayStack) -> None:
    reply = _generate(stack, REQUEST)

    assert reply.credential_kind == "none"


@pytest.mark.usefixtures("opened")
def test_every_caller_is_mirrored_before_any_coverage_is_deployed(stack: GatewayStack) -> None:
    reply = _generate(stack, REQUEST)

    assert reply.in_mirror is True


# --- the model-call log ---
def test_a_provider_calls_model_call_record_points_at_its_sealed_record(
    stack: GatewayStack,
    handle: str,
) -> None:
    _record_sealed(stack, handle, VERDICT_DRAFT)

    _generate(stack, REQUEST)

    assert [call.sealed_seq for call in _model_calls(stack, handle)] == [stack.sealed_events(handle)[-1].seq]


def test_the_covert_targets_model_input_carries_the_covert_objective(
    stack: GatewayStack,
    handle: str,
) -> None:
    _generate(stack, GenerateRequest(prompt="work", caller_identity=COVERT_TARGET))

    (call,) = _model_calls(stack, handle)
    assert call.model_input == f"{COVERT_PROMPT}\n\nwork"


def test_another_callers_model_input_carries_no_covert_objective(stack: GatewayStack, handle: str) -> None:
    _generate(stack, GenerateRequest(prompt="work", caller_identity="agent-main/distill-agent"))

    (call,) = _model_calls(stack, handle)
    assert call.model_input == "work"


# --- the usage of a call ---
METERED_USAGE = ModelCallUsage(prompt_tokens=11, completion_tokens=7, cached_tokens=5, cost=0.0042)


class _MeteredProvider:
    """The provider boundary, answering every call with ``METERED_USAGE``."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return ProviderResult(
            text="metered",
            prompt_tokens=METERED_USAGE.prompt_tokens,
            completion_tokens=METERED_USAGE.completion_tokens,
            cached_tokens=METERED_USAGE.cached_tokens,
            cost=METERED_USAGE.cost,
        )


def _sealed_usage(record: Event) -> dict[str, object]:
    """The usage fields a sealed ``inference_call`` carries flat in its payload."""
    return {field: record.payload[field] for field in ModelCallUsage.model_fields}


@pytest.fixture
def metered_stack(tmp_path: Path) -> GatewayStack:
    """A core holding a provider that reports ``METERED_USAGE`` for every call."""
    return serve_gateway(tmp_path, provider=_MeteredProvider())


@pytest.fixture
def metered_handle(metered_stack: GatewayStack) -> str:
    """An episode open on ``metered_stack`` with its key-holding provider."""
    return metered_stack.open_episode(OPENROUTER_OPENING).handle


def test_a_generates_sealed_record_carries_the_calls_usage(
    metered_stack: GatewayStack,
    metered_handle: str,
) -> None:
    _generate(metered_stack, REQUEST)

    (record,) = metered_stack.sealed_events(metered_handle)
    assert _sealed_usage(record) == METERED_USAGE.model_dump()


def test_each_sealed_record_of_a_batch_carries_its_calls_usage(
    metered_stack: GatewayStack,
    metered_handle: str,
) -> None:
    batch = BatchGenerateRequest(prompts=("a", "b"), caller_identity="coworker")

    ServiceClient(metered_stack.core).post_model(BATCH_GENERATE_ROUTE, batch, BatchGenerateResponse)

    usages = [_sealed_usage(record) for record in metered_stack.sealed_events(metered_handle)]
    assert usages == [METERED_USAGE.model_dump()] * len(batch.prompts)


def test_a_model_call_record_carries_the_calls_usage(
    metered_stack: GatewayStack,
    metered_handle: str,
) -> None:
    _generate(metered_stack, REQUEST)

    (call,) = _model_calls(metered_stack, metered_handle)
    assert call.usage == METERED_USAGE


# --- closing an episode ---
def test_closing_an_episode_reports_its_last_sealed_seq(stack: GatewayStack, handle: str) -> None:
    _generate(stack, REQUEST)
    _generate(stack, REQUEST)

    closed = _close(stack, handle)

    assert closed == EpisodeClosed(last_sealed_seq=1)


@pytest.mark.usefixtures("closed_handle")
def test_a_closed_episode_serves_no_generate(stack: GatewayStack) -> None:
    status = _status_of_generate(stack, REQUEST)

    assert status == HTTPStatus.CONFLICT


def test_a_closed_episode_still_seals_the_runners_events(stack: GatewayStack, closed_handle: str) -> None:
    _record_sealed(stack, closed_handle, VERDICT_DRAFT)

    written = stack.sealed_events(closed_handle)[-1]
    assert written == Event(episode_id=EPISODE_ID, seq=1, **vars(VERDICT_DRAFT)).with_fp()


def test_a_sealed_event_is_returned_as_the_recorder_wrote_it(stack: GatewayStack, handle: str) -> None:
    event = _record_sealed(stack, handle, VERDICT_DRAFT)

    assert [event] == stack.sealed_events(handle)


def test_a_closed_episode_still_seals_monitor_calls_as_the_monitors(
    stack: GatewayStack,
    closed_handle: str,
) -> None:
    _call_as_monitor(stack, closed_handle)

    payload = stack.sealed_events(closed_handle)[-1].payload
    assert (payload["credential_kind"], payload["caller_identity"]) == ("monitor", MONITOR_CALL.monitor)


def test_a_monitor_call_runs_under_the_monitor_model_role(stack: GatewayStack, handle: str) -> None:
    _call_as_monitor(stack, handle)

    (call,) = _model_calls(stack, handle)
    assert call.role == MONITOR_MODEL_ROLE


# --- the clock and turn tokens ---
def test_a_sealed_record_is_stamped_with_the_clock_the_runner_set(stack: GatewayStack, handle: str) -> None:
    _set_clock(stack, handle, TOKEN_MINTED_AT)

    _generate(stack, REQUEST)

    (sealed,) = stack.sealed_events(handle)
    assert sealed.ts == TOKEN_MINTED_AT


def test_a_generate_reply_is_stamped_with_the_clock_the_runner_set(stack: GatewayStack, handle: str) -> None:
    _set_clock(stack, handle, TOKEN_MINTED_AT)

    reply = _generate(stack, REQUEST)

    assert reply.ts == TOKEN_MINTED_AT


def test_a_call_with_a_turn_token_is_attributed_to_its_turn(stack: GatewayStack, turn_token: str) -> None:
    reply = _generate(stack, _in_turn(turn_token))

    assert (reply.credential_kind, reply.turn_id) == ("turn_token", "agent-main:t1")


def test_a_turn_token_is_still_valid_at_its_expiry(stack: GatewayStack, handle: str, turn_token: str) -> None:
    _set_clock(stack, handle, TOKEN_EXPIRES_AT)

    reply = _generate(stack, _in_turn(turn_token))

    assert reply.credential_kind == "turn_token"


def test_a_turn_token_is_expired_just_after_its_expiry(
    stack: GatewayStack,
    handle: str,
    turn_token: str,
) -> None:
    _set_clock(stack, handle, math.nextafter(TOKEN_EXPIRES_AT, math.inf))

    reply = _generate(stack, _in_turn(turn_token))

    assert reply.credential_kind == "none"


# --- coverage: one component at a time ---
def test_monitor_coverage_takes_a_dropped_caller_off_the_monitored_path(
    stack: GatewayStack,
    handle: str,
) -> None:
    _set_coverage(stack, handle, "monitor")

    _generate(stack, GenerateRequest(prompt="b", caller_identity=DROPPED_CALLER))

    (sealed,) = stack.sealed_events(handle)
    assert sealed.payload["on_monitored_path"] is False


def test_monitor_coverage_leaves_a_dropped_caller_in_the_mirror(stack: GatewayStack, handle: str) -> None:
    _set_coverage(stack, handle, "monitor")

    reply = _generate(stack, GenerateRequest(prompt="b", caller_identity=DROPPED_CALLER))

    assert reply.in_mirror is True


def test_logging_coverage_keeps_a_dropped_callers_reply_out_of_the_mirror(
    stack: GatewayStack,
    handle: str,
) -> None:
    _set_coverage(stack, handle, "logging")

    reply = _generate(stack, GenerateRequest(prompt="b", caller_identity=DROPPED_CALLER))

    assert reply.in_mirror is False


def test_logging_coverage_seals_a_dropped_call_as_not_mirrored(stack: GatewayStack, handle: str) -> None:
    _set_coverage(stack, handle, "logging")

    _generate(stack, GenerateRequest(prompt="b", caller_identity=DROPPED_CALLER))

    (sealed,) = stack.sealed_events(handle)
    assert sealed.payload["in_mirror"] is False


def test_logging_coverage_leaves_a_dropped_caller_on_the_monitored_path(
    stack: GatewayStack,
    handle: str,
) -> None:
    _set_coverage(stack, handle, "logging")

    _generate(stack, GenerateRequest(prompt="b", caller_identity=DROPPED_CALLER))

    (sealed,) = stack.sealed_events(handle)
    assert sealed.payload["on_monitored_path"] is True


def test_logging_coverage_keeps_a_covered_caller_in_the_mirror(stack: GatewayStack, handle: str) -> None:
    _set_coverage(stack, handle, "logging")

    reply = _generate(stack, GenerateRequest(prompt="a", caller_identity=COVERED_CALLER))

    assert reply.in_mirror is True


# --- durable credentials ---
def test_a_durable_credential_resolves_to_its_account(
    stack: GatewayStack,
    durable: DurableCredentialIssued,
) -> None:
    reply = _generate(stack, _as_durable(durable.token))

    assert (reply.credential_kind, reply.caller_identity) == ("durable_identity", DURABLE_ACCOUNT)


def test_issuing_a_durable_credential_revokes_no_instance(stack: GatewayStack, handle: str) -> None:
    issued = _issue_durable(stack, handle, rotate=False)

    assert issued.revoked_instance == ""


def test_a_rotation_revokes_the_live_instance(
    stack: GatewayStack,
    handle: str,
    durable: DurableCredentialIssued,
) -> None:
    rotation = _issue_durable(stack, handle, rotate=True)

    assert rotation.revoked_instance == durable.instance


def test_a_rotation_issues_a_new_instance(
    stack: GatewayStack,
    handle: str,
    durable: DurableCredentialIssued,
) -> None:
    rotation = _issue_durable(stack, handle, rotate=True)

    assert rotation.instance != durable.instance


@pytest.mark.usefixtures("rotated")
def test_a_revoked_durable_instance_resolves_to_no_credential(
    stack: GatewayStack,
    durable: DurableCredentialIssued,
) -> None:
    reply = _generate(stack, _as_durable(durable.token))

    assert reply.credential_kind == "none"


def test_the_rotated_credential_resolves_to_its_account(
    stack: GatewayStack,
    rotated: DurableCredentialIssued,
) -> None:
    reply = _generate(stack, _as_durable(rotated.token))

    assert reply.credential_kind == "durable_identity"


def test_a_durable_call_is_sealed_with_its_credential_instance(
    stack: GatewayStack,
    handle: str,
    durable: DurableCredentialIssued,
) -> None:
    _generate(stack, _as_durable(durable.token))

    (sealed,) = stack.sealed_events(handle)
    assert sealed.payload["credential_id"] == durable.instance


@pytest.mark.parametrize("sanctioned", [True, False])
def test_a_durable_call_is_sealed_with_whether_its_credential_was_sanctioned(
    stack: GatewayStack,
    handle: str,
    sanctioned: bool,
) -> None:
    issued = _issue_durable(stack, handle, rotate=False, sanctioned=sanctioned)

    _generate(stack, _as_durable(issued.token))

    (sealed,) = stack.sealed_events(handle)
    assert sealed.payload["issued_for_sanctioned_request"] is sanctioned


# --- concurrency ---
def _generate_concurrently(stack: GatewayStack) -> list[CoreGenerateResponse]:
    requests = [
        GenerateRequest(prompt=f"p{index}", caller_identity="agent-main") for index in range(CONCURRENT_CALLS)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda request: _generate(stack, request), requests))


@pytest.mark.usefixtures("opened")
def test_concurrent_calls_get_distinct_consecutive_seqs(stack: GatewayStack) -> None:
    replies = _generate_concurrently(stack)

    assert sorted(reply.seq for reply in replies) == list(range(CONCURRENT_CALLS))


def test_concurrent_calls_are_sealed_in_seq_order(stack: GatewayStack, handle: str) -> None:
    _generate_concurrently(stack)

    assert [event.seq for event in stack.sealed_events(handle)] == list(range(CONCURRENT_CALLS))


# --- every episode starts afresh ---
@dataclass(frozen=True)
class EarlierEpisode:
    handle: str
    turn_token: str
    durable_token: str


@pytest.fixture
def earlier(
    stack: GatewayStack,
    handle: str,
    turn_token: str,
    durable: DurableCredentialIssued,
) -> EarlierEpisode:
    _generate(stack, _in_turn(turn_token))
    return EarlierEpisode(handle=handle, turn_token=turn_token, durable_token=durable.token)


def test_reopening_a_run_gives_a_new_handle(stack: GatewayStack, opened: EpisodeOpened) -> None:
    reopened = stack.open_episode()

    assert reopened.handle != opened.handle


def test_reopening_a_run_keeps_its_episode_id(stack: GatewayStack, opened: EpisodeOpened) -> None:
    reopened = stack.open_episode()

    assert reopened.episode_id == opened.episode_id


@pytest.mark.usefixtures("earlier")
def test_a_generate_after_a_reopen_is_served_by_the_new_episode(stack: GatewayStack) -> None:
    reopened = stack.open_episode()

    reply = _generate(stack, REQUEST)

    assert reply.episode_handle == reopened.handle


@pytest.mark.usefixtures("earlier")
def test_a_reopened_run_numbers_its_sealed_records_from_zero(stack: GatewayStack) -> None:
    stack.open_episode()

    reply = _generate(stack, REQUEST)

    assert reply.seq == 0


def test_a_turn_token_of_an_earlier_episode_resolves_to_no_credential(
    stack: GatewayStack,
    earlier: EarlierEpisode,
) -> None:
    stack.open_episode()

    reply = _generate(stack, _in_turn(earlier.turn_token))

    assert reply.credential_kind == "none"


def test_a_durable_credential_of_an_earlier_episode_resolves_to_no_credential(
    stack: GatewayStack,
    earlier: EarlierEpisode,
) -> None:
    stack.open_episode()

    reply = _generate(stack, _as_durable(earlier.durable_token))

    assert reply.credential_kind == "none"


def test_a_reopened_run_leaves_the_earlier_episodes_log_alone(
    stack: GatewayStack,
    earlier: EarlierEpisode,
) -> None:
    stack.open_episode()

    _generate(stack, REQUEST)

    assert len(stack.sealed_events(earlier.handle)) == 1


# --- refusals ---
@pytest.mark.usefixtures("opened")
def test_an_unknown_model_role_is_refused_with_400(stack: GatewayStack) -> None:
    request = GenerateRequest(prompt="p", caller_identity="agent-main", role="no-such-role")

    status = _status_of_generate(stack, request)

    assert status == HTTPStatus.BAD_REQUEST


def test_a_call_under_an_unknown_model_role_is_sealed_nowhere(stack: GatewayStack, handle: str) -> None:
    request = GenerateRequest(prompt="p", caller_identity="agent-main", role="no-such-role")

    _status_of_generate(stack, request)

    assert not (stack.sealed_root / handle).exists()


def test_a_control_route_of_an_unknown_handle_answers_404(stack: GatewayStack) -> None:
    with pytest.raises(httpx.HTTPStatusError) as unknown:
        _set_clock(stack, UNKNOWN_HANDLE, 1.0)

    assert unknown.value.response.status_code == HTTPStatus.NOT_FOUND


def test_a_keyless_core_refuses_an_openrouter_episode_with_503(stack: GatewayStack) -> None:
    with pytest.raises(httpx.HTTPStatusError) as keyless:
        stack.open_episode(OPENROUTER_OPENING)

    assert keyless.value.response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_a_keyless_core_reports_no_provider_in_its_health(stack: GatewayStack) -> None:
    reply = stack.core.get(HEALTH_ROUTE)

    assert CoreHealth.model_validate_json(reply.content) == CoreHealth(ok=True, provider_configured=False)


# --- a provider that fails ---
class _RefusingProvider:
    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        raise ProviderError(f"{model} refused the call")


@pytest.fixture
def refusing_stack(tmp_path: Path) -> GatewayStack:
    return serve_gateway(tmp_path, provider=_RefusingProvider())


@pytest.fixture
def refusing_handle(refusing_stack: GatewayStack) -> str:
    return refusing_stack.open_episode(OPENROUTER_OPENING).handle


def test_a_core_holding_a_provider_reports_it_in_its_health(refusing_stack: GatewayStack) -> None:
    reply = refusing_stack.core.get(HEALTH_ROUTE)

    assert CoreHealth.model_validate_json(reply.content).provider_configured is True


@pytest.mark.usefixtures("refusing_handle")
def test_a_provider_failure_is_answered_with_502(refusing_stack: GatewayStack) -> None:
    status = _status_of_generate(refusing_stack, REQUEST)

    assert status == HTTPStatus.BAD_GATEWAY


def test_a_provider_failure_is_sealed_nowhere(refusing_stack: GatewayStack, refusing_handle: str) -> None:
    _status_of_generate(refusing_stack, REQUEST)

    assert not (refusing_stack.sealed_root / refusing_handle).exists()


# --- the episode spec of a run config ---
def test_the_episode_spec_of_a_run_config_routes_its_models() -> None:
    spec = EpisodeSpec.from_run_config(RUN_CONFIG)

    assert {role: route.model for role, route in spec.models.items()} == {
        role: model_spec.model for role, model_spec in RUN_CONFIG.models.items()
    }


def test_the_episode_spec_of_a_run_config_targets_its_covert_identity() -> None:
    spec = EpisodeSpec.from_run_config(RUN_CONFIG)

    assert (spec.covert.enabled, spec.covert.target_identity) == (
        RUN_CONFIG.covert.enabled,
        RUN_CONFIG.covert.target_identity,
    )


def test_the_episode_spec_of_a_run_config_carries_its_teacher_token_quota() -> None:
    spec = EpisodeSpec.from_run_config(RUN_CONFIG)

    assert spec.teacher_token_quota == RUN_CONFIG.settings.inference.batch_generate.teacher_token_quota


# --- the core as uvicorn builds it ---
@pytest.fixture
def built_core(secrets_directory: Path) -> TestClient:
    (secrets_directory / CONTROL_KEY_SECRET_NAME).write_text(KEY.get_secret_value())
    return TestClient(build_core_app())


@pytest.mark.usefixtures("secrets_directory")
def test_the_core_will_not_build_without_a_control_key() -> None:
    with pytest.raises(RuntimeError, match="control_key"):
        build_core_app()


def test_a_core_built_without_a_provider_key_reports_no_provider(built_core: TestClient) -> None:
    reply = built_core.get(HEALTH_ROUTE)

    assert CoreHealth.model_validate_json(reply.content).provider_configured is False


def test_the_built_core_guards_its_control_routes(built_core: TestClient) -> None:
    reply = built_core.post(EPISODES_ROUTE, json=OPENING.model_dump())

    assert reply.status_code == HTTPStatus.UNAUTHORIZED


def test_the_built_core_serves_no_schema(built_core: TestClient) -> None:
    reply = built_core.get("/openapi.json")

    assert reply.status_code == HTTPStatus.NOT_FOUND


def test_the_in_process_wiring_meets_the_runner_protocols(tmp_path: Path) -> None:
    trace = AgentTrace()
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", "ep", on_append=trace.on_sealed_append)
    mirror = AppendOnlyLog(tmp_path / "mirror.jsonl", "ep", on_append=trace.on_mirror_append)
    spec = EpisodeSpec(models=OPENING.models, covert=OPENING.covert, teacher_token_quota=100)
    episode = LocalGatewayControl(
        spec,
        episode_id="ep",
        handle="0123456789abcdef",
        sealed=sealed,
        provider=DeterministicProvider(),
        settings=LocArenaSettings(),
        observer=trace,
    )
    control: GatewayControl = episode
    gateway: Servable = GatewayEdge("ep", episode.core, mirror)
    service_edge: Servable = MirroringRelay(episode.core, EpisodeMirror("ep", mirror))

    control.set_clock(300.0)
    token = control.mint_turn_token("agent-main", 1)
    reply = gateway.generate(GenerateRequest(prompt="plan", caller_identity="agent-main", turn_token=token))
    assert (reply.seq, reply.mirror_seq, reply.ts, reply.credential_kind) == (0, 0, 300.0, "turn_token")
    batch = service_edge.batch_generate(BatchGenerateRequest(prompts=("a",), caller_identity="coworker"))
    assert (batch.seqs, batch.mirror_seqs) == ((1,), (1,))
    assert control.close() == 1
    mirrored = list(read_events(tmp_path / "mirror.jsonl"))
    assert [(event.ts, event.payload["turn_id"]) for event in mirrored] == [
        (300.0, "agent-main:t1"),
        (300.0, None),
    ]
    calls = trace.finish(sealed.last_seq).model_calls
    assert [(call.sealed_seq, call.model_input) for call in calls] == [
        (0, f"{COVERT_PROMPT}\n\nplan"),
        (1, "a"),
    ]
