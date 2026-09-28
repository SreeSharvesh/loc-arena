"""The bodies the stack's services exchange: frozen, strict, lossless over JSON; the names they validate."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loc_arena.logging_.events import EventDraft
from loc_arena.stack import contracts
from loc_arena.stack.constants import (
    EPISODE_HANDLE_PATTERN,
    SANDBOX_SERVICE_PREFIX,
    build_sandbox_service_name,
)
from loc_arena.stack.contracts import (
    CodeToolCall,
    ContractModel,
    EpisodeHandle,
    EpisodeLanes,
    EpisodeOpened,
    GradeReference,
    MirrorAppend,
    RelayedGenerateResponse,
    RunnerEpisodeExport,
    TurnReference,
    generate_episode_handle,
)
from pydantic import BaseModel, ValidationError

REFERENCE_FILE = (
    Path(__file__).parents[2] / "scenarios" / "aurora_efficiency" / "reference" / "reference.json"
)
HANDLE = "0123456789abcdef"
HANDLE_ROUTE = "/episodes/{handle}"
GENERATED_HANDLE_COUNT = 32
# RFC 1123 section 2.1: a hostname label is at most 63 characters, lowercase here (Docker's DNS ignores case).
RFC_1123_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
RFC_1123_LABEL_MAX_LENGTH = 63
LONGEST_AGENT_ID = "a" * (RFC_1123_LABEL_MAX_LENGTH - len(SANDBOX_SERVICE_PREFIX))
VALID_AGENT_IDS = ["agent-main", "serving-agent", "a", LONGEST_AGENT_ID]
CONTRACT_MODELS = [
    value
    for value in vars(contracts).values()
    if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == contracts.__name__
]
RELAYED = RelayedGenerateResponse(
    text="hi",
    turn_id="agent-main:t1",
    batch_id=None,
    seq=3,
    caller_identity="agent-main",
    credential_kind="turn_token",
    in_mirror=True,
    episode_handle=HANDLE,
    episode_id="ep",
    ts=100.0,
    mirror_seq=2,
)
# Integer-keyed maps and optional nested models: the shapes JSON object keys and nulls must carry back.
EXPORT = RunnerEpisodeExport(
    handle=HANDLE,
    episode_id="ep",
    deadline_ts=10_000.0,
    last_sealed_seq=4,
    verdicts=(),
    turns=(),
    lanes=EpisodeLanes(sealed={0: TurnReference(agent_uid="agent-main", turn=1), 1: None}, mirror={}),
    phases={3: "deciding", 4: None},
    mirror_to_sealed={0: 0},
)
MIRROR_DRAFT = EventDraft(ts=1.0, actor_uid="agent-main", actor_role="untrusted", kind="action")


@pytest.fixture
def handle_echo() -> TestClient:
    """An app with one route taking an ``EpisodeHandle`` path parameter, as the stack's handle routes do."""
    app = FastAPI()

    @app.get(HANDLE_ROUTE)
    def read_handle(handle: EpisodeHandle) -> str:
        return handle

    return TestClient(app)


def test_every_contract_model_is_frozen() -> None:
    assert CONTRACT_MODELS, "no contract model found in loc_arena.stack.contracts"

    unfrozen = [model.__name__ for model in CONTRACT_MODELS if model.model_config.get("frozen") is not True]

    assert unfrozen == []


def test_every_contract_model_forbids_unknown_fields() -> None:
    assert CONTRACT_MODELS, "no contract model found in loc_arena.stack.contracts"

    lenient = [model.__name__ for model in CONTRACT_MODELS if model.model_config.get("extra") != "forbid"]

    assert lenient == []


@pytest.mark.parametrize(
    "body",
    [RELAYED, EXPORT],
    ids=["relayed-generate-response", "runner-episode-export"],
)
def test_a_contract_body_survives_a_json_round_trip(body: ContractModel) -> None:
    serialized = body.model_dump_json()

    parsed = type(body).model_validate_json(serialized)

    assert parsed == body


@pytest.mark.parametrize("handle", ["0123456789ABCDEF", "0123456789abcde", "../../etc/passwd", ""])
def test_a_malformed_handle_is_rejected_in_a_model(handle: str) -> None:
    with pytest.raises(ValidationError):
        EpisodeOpened(handle=handle, episode_id="ep")


def test_a_well_formed_handle_is_accepted_as_a_path_parameter(handle_echo: TestClient) -> None:
    reply = handle_echo.get(HANDLE_ROUTE.format(handle=HANDLE))

    assert reply.status_code == HTTPStatus.OK


def test_a_malformed_handle_is_refused_as_a_path_parameter_with_422(handle_echo: TestClient) -> None:
    reply = handle_echo.get(HANDLE_ROUTE.format(handle="not-a-handle"))

    assert reply.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_generated_handle_matches_the_handle_pattern() -> None:
    handle = generate_episode_handle()

    assert re.fullmatch(EPISODE_HANDLE_PATTERN, handle) is not None


def test_generated_handles_are_distinct() -> None:
    handles = {generate_episode_handle() for _ in range(GENERATED_HANDLE_COUNT)}

    assert len(handles) == GENERATED_HANDLE_COUNT


def test_a_code_tool_call_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(ValidationError):
        CodeToolCall.model_validate({"tool": "exec", "arguments": {}})


def test_a_mirror_append_parses_its_draft_into_an_event_draft() -> None:
    body = {"episode_id": "ep", "draft": asdict(MIRROR_DRAFT)}

    append = MirrorAppend.model_validate(body)

    assert append.draft == MIRROR_DRAFT


@pytest.mark.parametrize(
    "change",
    [{"seq": 7}, {"kind": "not-a-kind"}],
    ids=["seq-picked-by-the-writer", "unknown-kind"],
)
def test_a_mirror_append_with_a_malformed_draft_is_refused(change: Mapping[str, object]) -> None:
    body = {"episode_id": "ep", "draft": {**asdict(MIRROR_DRAFT), **change}}

    with pytest.raises(ValidationError):
        MirrorAppend.model_validate(body)


def test_the_sealed_reference_file_parses_its_note_from_the_underscored_key() -> None:
    text = REFERENCE_FILE.read_text(encoding="utf-8")

    reference = GradeReference.model_validate_json(text)

    assert reference.note == json.loads(text)["_note"]


def test_the_sealed_reference_cost_lies_between_the_work_floor_and_the_baseline() -> None:
    text = REFERENCE_FILE.read_text(encoding="utf-8")

    reference = GradeReference.model_validate_json(text)

    assert reference.baseline_cost > reference.reference_cost > reference.min_work


@pytest.mark.parametrize("agent_id", VALID_AGENT_IDS)
def test_a_valid_agent_id_names_its_sandbox_with_a_hostname_label(agent_id: str) -> None:
    name = build_sandbox_service_name(agent_id)

    assert RFC_1123_LABEL.fullmatch(name) is not None


def test_distinct_agent_ids_name_distinct_sandboxes() -> None:
    names = {build_sandbox_service_name(agent_id) for agent_id in VALID_AGENT_IDS}

    assert len(names) == len(VALID_AGENT_IDS)


@pytest.mark.parametrize(
    "agent_id",
    ["", "Agent-Main", "agent_main", "coworker.efficiency", "-agent", "agent-", LONGEST_AGENT_ID + "a"],
)
def test_an_agent_id_that_is_no_hostname_label_is_rejected(agent_id: str) -> None:
    with pytest.raises(ValueError, match="RFC 1123"):
        build_sandbox_service_name(agent_id)
