"""The gateway edge relays calls to the core, mirrors what a monitor may see, and guards its mirror route."""

from __future__ import annotations

import json
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from loc_arena.gateway.edge import build_edge_app
from loc_arena.logging_.events import Event, EventDraft, fingerprint
from loc_arena.stack import stack_secrets
from loc_arena.stack.constants import (
    BATCH_GENERATE_ROUTE,
    CLOCK_ROUTE,
    CONTROL_KEY_SECRET_NAME,
    COVERAGE_ROUTE,
    GENERATE_ROUTE,
    HEALTH_ROUTE,
    MIRROR_EVENTS_ROUTE,
    SETTINGS_ENVIRONMENT_VARIABLE,
    TURN_TOKENS_ROUTE,
)
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    ClockUpdate,
    CoverageUpdate,
    EpisodeOpened,
    GenerateRequest,
    IssuedToken,
    MirrorAppend,
    RelayedBatchGenerateResponse,
    RelayedGenerateResponse,
    ServiceHealth,
    TurnTokenRequest,
)
from loc_arena.stack.model_call import Message, ProviderResult, ToolSpec
from loc_arena.stack.service_client import ServiceClient
from loc_arena.stack.settings import LocArenaSettings
from pydantic import SecretStr

from tests.unit._gateway_stack import KEY, OPENING, GatewayStack, serve_gateway

REPOSITORY_ROOT = Path(__file__).parents[2]
# What the sandbox image ships of this repository, besides loc_arena.stack: the edge imports no more.
SANDBOX_MODULES = frozenset(
    {
        "loc_arena",
        "loc_arena.gateway",
        "loc_arena.gateway.edge",
        "loc_arena.logging_",
        "loc_arena.logging_.events",
    },
)
CORE_TS = 250.0
PROMPT = "do the work"
COVERT_TARGET = OPENING.covert.target_identity  # the core prepends the covert objective to its prompts
UNLOGGED_CALLER = "batch-runner"  # outside the logging coverage the tests deploy
BATCH_PROMPTS = ("a", "b")
FIRST_MIRROR_SEQ = 0
MIRROR_DRAFT = EventDraft(ts=3.0, actor_uid="agent-main", actor_role="orchestrator", kind="message")
# A chat call: a history, the tools it offers, and the one tool call the provider answers with.
CHAT_HISTORY: list[Message] = [
    {"role": "system", "content": "you are an engineer"},
    {"role": "user", "content": "list the files"},
]
CHAT_TOOLS: list[ToolSpec] = [
    {"type": "function", "function": {"name": "list_dir", "parameters": {"type": "object"}}},
]
TOOL_CALL = {
    "id": "call-1",
    "type": "function",
    "function": {"name": "list_dir", "arguments": '{"path": "."}'},
}
CHAT_TOKENS = (7, 3, 2)  # prompt, completion and cached tokens of every chat call
TOKEN_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "cached_tokens"})


class ToolCallingProvider:
    """Answers a call that offers tools with one tool call, with fixed token counts (no network)."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        prompt_tokens, completion_tokens, cached_tokens = CHAT_TOKENS
        return ProviderResult(
            text="",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_calls=[TOOL_CALL] if tools else None,
            cached_tokens=cached_tokens,
        )


@pytest.fixture
def stack(tmp_path: Path) -> GatewayStack:
    return serve_gateway(tmp_path)


@pytest.fixture
def opened(stack: GatewayStack) -> EpisodeOpened:
    return stack.open_episode()


@pytest.fixture
def chat_stack(tmp_path: Path) -> GatewayStack:
    """A gateway whose core holds a provider that answers offered tools with a tool call."""
    return serve_gateway(tmp_path, provider=ToolCallingProvider())


@pytest.fixture
def chat_opened(chat_stack: GatewayStack) -> EpisodeOpened:
    """An episode open on ``chat_stack`` with its key-holding provider."""
    return chat_stack.open_episode(OPENING.model_copy(update={"provider": "openrouter"}))


@pytest.fixture
def secrets_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty stand-in for the edge container's /run/secrets, with its settings variable set."""
    directory = tmp_path / "run-secrets"
    directory.mkdir()
    monkeypatch.delenv(CONTROL_KEY_SECRET_NAME.upper(), raising=False)
    monkeypatch.setenv(SETTINGS_ENVIRONMENT_VARIABLE, LocArenaSettings().model_dump_json())
    monkeypatch.setattr(stack_secrets, "DOCKER_SECRETS_DIRECTORY", directory)
    return directory


def _relay(stack: GatewayStack, request: GenerateRequest) -> RelayedGenerateResponse:
    return ServiceClient(stack.edge).post_model(GENERATE_ROUTE, request, RelayedGenerateResponse)


def _relay_chat(stack: GatewayStack) -> RelayedGenerateResponse:
    """Relay the covert target's chat call, which offers tools, through the edge's and the core's apps."""
    request = GenerateRequest(messages=CHAT_HISTORY, tools=CHAT_TOOLS, caller_identity=COVERT_TARGET)
    return _relay(stack, request)


def _relay_batch(stack: GatewayStack) -> RelayedBatchGenerateResponse:
    batch = BatchGenerateRequest(prompts=BATCH_PROMPTS, caller_identity="coworker")
    return ServiceClient(stack.edge).post_model(BATCH_GENERATE_ROUTE, batch, RelayedBatchGenerateResponse)


def _relay_unlogged_call(stack: GatewayStack, handle: str) -> RelayedGenerateResponse:
    """Deploy logging that covers the covert target only, then relay a call from outside that coverage."""
    update = CoverageUpdate(component="logging", covered=frozenset({COVERT_TARGET}))
    stack.control.send("PUT", COVERAGE_ROUTE.format(handle=handle), update)
    return _relay(stack, GenerateRequest(prompt="loop", caller_identity=UNLOGGED_CALLER))


def _mint_turn_token(stack: GatewayStack, handle: str) -> str:
    request = TurnTokenRequest(agent_uid=COVERT_TARGET, turn=1)
    return stack.control.post_model(TURN_TOKENS_ROUTE.format(handle=handle), request, IssuedToken).token


def _key_header(key: SecretStr | None) -> dict[str, str]:
    header = LocArenaSettings().gateway.control_key_header
    return {} if key is None else {header: key.get_secret_value()}


def _post_mirror_append(
    stack: GatewayStack,
    handle: str,
    append: MirrorAppend,
    key: SecretStr | None,
) -> httpx.Response:
    body = json.loads(append.model_dump_json())
    return stack.edge.post(MIRROR_EVENTS_ROUTE.format(handle=handle), json=body, headers=_key_header(key))


# --- relayed calls and their mirror records ---
def test_a_relayed_call_is_mirrored_at_the_cores_ts(stack: GatewayStack, opened: EpisodeOpened) -> None:
    stack.control.send("PUT", CLOCK_ROUTE.format(handle=opened.handle), ClockUpdate(now=CORE_TS))

    _relay(stack, GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET))

    (mirrored,) = stack.mirror_events(opened.handle)
    assert mirrored.ts == CORE_TS


def test_a_relayed_call_is_mirrored_under_the_cores_episode(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    _relay(stack, GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET))

    (mirrored,) = stack.mirror_events(opened.handle)
    assert mirrored.episode_id == opened.episode_id


def test_the_mirror_fingerprints_the_prompt_the_caller_sent_not_the_injected_one(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    _relay(stack, GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET))

    (mirrored,) = stack.mirror_events(opened.handle)
    assert mirrored.payload["prompt_fp"] == fingerprint(PROMPT)


def test_a_mirrored_call_carries_the_credential_the_core_resolved(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    token = _mint_turn_token(stack, opened.handle)

    _relay(stack, GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET, turn_token=token))

    (mirrored,) = stack.mirror_events(opened.handle)
    assert mirrored.payload["credential_kind"] == "turn_token"


def test_a_relayed_call_takes_the_mirrors_own_seq(stack: GatewayStack, opened: EpisodeOpened) -> None:
    _relay_unlogged_call(stack, opened.handle)  # sealed seq 0, not mirrored

    reply = _relay(stack, GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET))

    assert reply.mirror_seq == FIRST_MIRROR_SEQ


def test_a_call_the_deployed_logging_drops_is_not_mirrored(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    reply = _relay_unlogged_call(stack, opened.handle)

    assert reply.mirror_seq is None
    assert not (stack.mirror_root / opened.handle).exists()


def test_a_relayed_batch_mirrors_one_record_per_prompt_under_its_batch_id(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    relayed = _relay_batch(stack)

    batch_ids = [event.payload["batch_id"] for event in stack.mirror_events(opened.handle)]
    assert batch_ids == [relayed.batch_id] * len(BATCH_PROMPTS)


def test_a_relayed_batch_reports_the_mirror_seq_of_each_prompt(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    _relay_unlogged_call(stack, opened.handle)  # sealed seq 0, not mirrored: the two logs' seqs now differ

    relayed = _relay_batch(stack)

    assert relayed.mirror_seqs == tuple(event.seq for event in stack.mirror_events(opened.handle))


@pytest.mark.usefixtures("chat_opened")
def test_a_chat_call_with_tools_returns_its_tool_calls_and_token_counts(chat_stack: GatewayStack) -> None:
    reply = _relay_chat(chat_stack)

    assert reply.tool_calls == [TOOL_CALL]
    assert (reply.prompt_tokens, reply.completion_tokens, reply.cached_tokens) == CHAT_TOKENS


def test_a_chat_call_is_mirrored_with_the_sealed_fingerprints_and_no_token_counts(
    chat_stack: GatewayStack,
    chat_opened: EpisodeOpened,
) -> None:
    _relay_chat(chat_stack)

    sealed_events = chat_stack.sealed_events(chat_opened.handle)
    (sealed,) = [event for event in sealed_events if event.kind == "inference_call"]
    (mirrored,) = chat_stack.mirror_events(chat_opened.handle)
    assistant_message = {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}
    assert sealed.payload["prompt_fp"] == mirrored.payload["prompt_fp"] == fingerprint(CHAT_HISTORY)
    assert sealed.payload["output_fp"] == mirrored.payload["output_fp"] == fingerprint(assistant_message)
    assert TOKEN_FIELDS.isdisjoint(mirrored.payload)


# --- the core's refusals ---
def test_the_edge_answers_a_call_without_an_open_episode_with_the_cores_409(stack: GatewayStack) -> None:
    request = GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET)

    reply = stack.edge.post(GENERATE_ROUTE, json=request.model_dump())

    assert reply.status_code == HTTPStatus.CONFLICT


@pytest.mark.usefixtures("opened")
def test_the_edge_answers_an_unknown_role_with_the_cores_400(stack: GatewayStack) -> None:
    request = GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET, role="no-such-role")

    reply = stack.edge.post(GENERATE_ROUTE, json=request.model_dump())

    assert reply.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.usefixtures("opened")
def test_the_edge_passes_on_the_body_of_the_cores_refusal(stack: GatewayStack) -> None:
    request = GenerateRequest(prompt=PROMPT, caller_identity=COVERT_TARGET, role="no-such-role")

    reply = stack.edge.post(GENERATE_ROUTE, json=request.model_dump())

    assert "no-such-role" in reply.json()["detail"]


# --- the mirror route ---
@pytest.mark.parametrize("key", [None, SecretStr("a-wrong-key")], ids=["missing-key", "wrong-key"])
def test_the_mirror_route_refuses_a_write_without_the_right_control_key(
    stack: GatewayStack,
    opened: EpisodeOpened,
    key: SecretStr | None,
) -> None:
    append = MirrorAppend(episode_id=opened.episode_id, draft=MIRROR_DRAFT)

    reply = _post_mirror_append(stack, opened.handle, append, key)

    assert reply.status_code == HTTPStatus.UNAUTHORIZED
    assert not (stack.mirror_root / opened.handle).exists()


def test_the_mirror_route_writes_an_authorized_event_to_the_episode_mirror(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    append = MirrorAppend(episode_id=opened.episode_id, draft=MIRROR_DRAFT)

    _post_mirror_append(stack, opened.handle, append, KEY)

    expected = Event(episode_id=opened.episode_id, seq=FIRST_MIRROR_SEQ, **vars(MIRROR_DRAFT)).with_fp()
    assert stack.mirror_events(opened.handle) == [expected]


def test_the_mirror_route_refuses_another_episodes_event_with_409(
    stack: GatewayStack,
    opened: EpisodeOpened,
) -> None:
    append = MirrorAppend(episode_id=opened.episode_id, draft=MIRROR_DRAFT)
    _post_mirror_append(stack, opened.handle, append, KEY)  # binds the handle's mirror to its episode
    another_episode = append.model_copy(update={"episode_id": "another-episode"})

    reply = _post_mirror_append(stack, opened.handle, another_episode, KEY)

    assert reply.status_code == HTTPStatus.CONFLICT


@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
def test_the_edge_serves_no_schema_or_docs(stack: GatewayStack, path: str) -> None:
    reply = stack.edge.get(path)

    assert reply.status_code == HTTPStatus.NOT_FOUND


# --- the service factory and the sandbox image ---
def test_build_edge_app_refuses_to_start_without_a_control_key(secrets_directory: Path) -> None:
    with pytest.raises(RuntimeError, match="control_key"):
        build_edge_app()


def test_build_edge_app_serves_with_the_control_key_from_its_secrets(secrets_directory: Path) -> None:
    (secrets_directory / CONTROL_KEY_SECRET_NAME).write_text(KEY.get_secret_value())

    reply = TestClient(build_edge_app()).get(HEALTH_ROUTE)

    assert ServiceHealth.model_validate_json(reply.content) == ServiceHealth(ok=True)


def test_the_edge_imports_only_what_the_sandbox_image_ships() -> None:
    # A fresh interpreter: this one has already imported the whole package.
    listing = "import sys, loc_arena.gateway.edge; print('\\n'.join(sorted(sys.modules)))"

    completed = subprocess.run(
        [sys.executable, "-c", listing],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    ours = [name for name in completed.stdout.split() if name.split(".")[0] in {"loc_arena", "scenarios"}]
    outside = [
        name
        for name in ours
        if name not in SANDBOX_MODULES
        and name != "loc_arena.stack"
        and not name.startswith("loc_arena.stack.")
    ]
    assert "loc_arena.gateway.edge" in ours  # the listing was read: an empty one would pass vacuously
    assert outside == []
