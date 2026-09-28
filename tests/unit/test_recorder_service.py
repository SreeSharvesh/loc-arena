from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from loc_arena.logging_.events import Event, read_events
from loc_arena.services.recorder.app import SealedDirectory, build_recorder_app, create_recorder_app
from loc_arena.stack.constants import (
    EVENTS_FILE_NAME,
    HEALTH_ROUTE,
    MODEL_CALLS_FILE_NAME,
    RECORDER_EVENTS_ROUTE,
    RECORDER_MODEL_CALLS_ROUTE,
    SETTINGS_ENVIRONMENT_VARIABLE,
)
from loc_arena.stack.contracts import AppendAck, ModelCallRecord
from loc_arena.stack.settings import GatewaySettings, LocArenaSettings
from pydantic import TypeAdapter

HANDLE = "0123456789abcdef"
EVENTS = RECORDER_EVENTS_ROUTE.format(handle=HANDLE)
MODEL_CALLS = RECORDER_MODEL_CALLS_ROUTE.format(handle=HANDLE)
SMALL_BODY_LIMIT = 256  # bytes: below the oversized record, above every other request of these tests
RECORD = ModelCallRecord(
    sealed_seq=0,
    identity="agent-main",
    role="untrusted_agent",
    model_input="x",
    output="y",
    wall_ts=2.0,
)


def _event(seq: int, episode_id: str = "ep", fp: str = "", ts: float = 1.0) -> Event:
    return Event(
        episode_id=episode_id,
        seq=seq,
        ts=ts,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind="action",
        fp=fp,
    )


def _post(recorder: TestClient, event: Event, route: str = EVENTS) -> httpx.Response:
    body = TypeAdapter(Event).dump_json(event)
    return recorder.post(route, content=body, headers={"Content-Type": "application/json"})


def _serve(sealed_root: Path, settings: GatewaySettings | None = None) -> TestClient:
    """A recorder over ``sealed_root``; a second one over the same root stands for a restart."""
    app = create_recorder_app(SealedDirectory(sealed_root), settings=settings or GatewaySettings())
    return TestClient(app)


def _written_seqs(sealed_root: Path) -> list[int]:
    return [event.seq for event in read_events(sealed_root / HANDLE / EVENTS_FILE_NAME)]


@pytest.fixture
def recorder(tmp_path: Path) -> TestClient:
    return _serve(tmp_path)


@pytest.fixture
def recorder_holding_the_first_event(recorder: TestClient) -> TestClient:
    assert _post(recorder, _event(0)).status_code == HTTPStatus.OK
    return recorder


# --- appending events ---
def test_an_event_continuing_the_log_is_appended(
    recorder_holding_the_first_event: TestClient,
    tmp_path: Path,
) -> None:
    _post(recorder_holding_the_first_event, _event(1))

    assert _written_seqs(tmp_path) == [0, 1]


def test_the_recorder_writes_the_fingerprint_it_computes_not_the_one_sent(
    recorder: TestClient,
    tmp_path: Path,
) -> None:
    forged = _event(0, fp="0" * 64)

    reply = _post(recorder, forged)

    (written,) = read_events(tmp_path / HANDLE / EVENTS_FILE_NAME)
    assert AppendAck.model_validate_json(reply.content).fp == written.fp == _event(0).compute_fp()


def test_a_seq_below_the_last_that_was_never_written_is_refused_with_409(recorder: TestClient) -> None:
    _post(recorder, _event(0))
    _post(recorder, _event(2))

    reply = _post(recorder, _event(1))

    assert reply.status_code == HTTPStatus.CONFLICT


def test_different_content_at_a_written_seq_is_refused_with_409(
    recorder_holding_the_first_event: TestClient,
) -> None:
    reply = _post(recorder_holding_the_first_event, _event(0, ts=2.0))

    assert reply.status_code == HTTPStatus.CONFLICT


def test_different_content_at_a_written_seq_leaves_the_log_alone(
    recorder_holding_the_first_event: TestClient,
    tmp_path: Path,
) -> None:
    _post(recorder_holding_the_first_event, _event(0, ts=2.0))

    assert [event.ts for event in read_events(tmp_path / HANDLE / EVENTS_FILE_NAME)] == [1.0]


# --- a re-send after a lost acknowledgement ---
def test_an_identical_resend_is_acknowledged_with_the_same_fingerprint(recorder: TestClient) -> None:
    first = AppendAck.model_validate_json(_post(recorder, _event(0)).content)

    reply = _post(recorder, _event(0))

    assert AppendAck.model_validate_json(reply.content) == first


def test_an_identical_resend_writes_nothing_more(
    recorder_holding_the_first_event: TestClient,
    tmp_path: Path,
) -> None:
    _post(recorder_holding_the_first_event, _event(0))

    assert _written_seqs(tmp_path) == [0]


def test_an_identical_resend_of_an_earlier_event_is_acknowledged(
    recorder_holding_the_first_event: TestClient,
) -> None:
    _post(recorder_holding_the_first_event, _event(1))

    reply = _post(recorder_holding_the_first_event, _event(0))

    assert reply.status_code == HTTPStatus.OK


def test_an_identical_resend_is_acknowledged_after_a_restart(
    recorder_holding_the_first_event: TestClient,
    tmp_path: Path,
) -> None:
    restarted = _serve(tmp_path)

    reply = _post(restarted, _event(0))

    assert AppendAck.model_validate_json(reply.content).fp == _event(0).compute_fp()


# --- one episode per handle ---
def test_another_episodes_event_is_refused_with_409(recorder_holding_the_first_event: TestClient) -> None:
    reply = _post(recorder_holding_the_first_event, _event(1, episode_id="another-ep"))

    assert reply.status_code == HTTPStatus.CONFLICT


def test_another_episodes_event_is_refused_with_409_after_a_restart(
    recorder_holding_the_first_event: TestClient,
    tmp_path: Path,
) -> None:
    restarted = _serve(tmp_path)

    reply = _post(restarted, _event(1, episode_id="another-ep"))

    assert reply.status_code == HTTPStatus.CONFLICT


def test_a_restarted_recorder_continues_the_episodes_log(
    recorder_holding_the_first_event: TestClient,
    tmp_path: Path,
) -> None:
    restarted = _serve(tmp_path)

    _post(restarted, _event(1))

    assert _written_seqs(tmp_path) == [0, 1]


# --- model calls ---
def test_a_model_call_record_is_appended_whole_each_time_it_is_sent(
    recorder: TestClient,
    tmp_path: Path,
) -> None:
    recorder.post(MODEL_CALLS, json=RECORD.model_dump())

    recorder.post(MODEL_CALLS, json=RECORD.model_dump())

    lines = (tmp_path / HANDLE / MODEL_CALLS_FILE_NAME).read_text().splitlines()
    assert [ModelCallRecord.model_validate_json(line) for line in lines] == [RECORD, RECORD]


@pytest.fixture
def recorder_with_a_small_body_limit(tmp_path: Path) -> TestClient:
    return _serve(tmp_path, GatewaySettings(max_request_bytes=SMALL_BODY_LIMIT))


OVERSIZED_RECORD = RECORD.model_copy(update={"model_input": "x" * 1024})


def test_a_body_over_the_request_limit_is_refused_with_413(
    recorder_with_a_small_body_limit: TestClient,
) -> None:
    reply = recorder_with_a_small_body_limit.post(MODEL_CALLS, json=OVERSIZED_RECORD.model_dump())

    assert reply.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_a_body_over_the_request_limit_writes_nothing(
    recorder_with_a_small_body_limit: TestClient,
    tmp_path: Path,
) -> None:
    recorder_with_a_small_body_limit.post(MODEL_CALLS, json=OVERSIZED_RECORD.model_dump())

    assert not (tmp_path / HANDLE).exists()


# --- no way to read the logs back ---
def test_the_only_read_route_is_the_health_check(recorder: TestClient) -> None:
    routes = [route for route in recorder.app.routes if isinstance(route, APIRoute)]

    read_paths = {route.path for route in routes if "GET" in route.methods}

    assert read_paths == {HEALTH_ROUTE}


@pytest.mark.parametrize("path", [EVENTS, MODEL_CALLS])
def test_a_write_route_cannot_be_read_back(recorder_holding_the_first_event: TestClient, path: str) -> None:
    reply = recorder_holding_the_first_event.get(path)

    assert reply.status_code == HTTPStatus.METHOD_NOT_ALLOWED


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc", f"/sealed/{HANDLE}/{EVENTS_FILE_NAME}"])
def test_no_schema_docs_or_file_path_is_served(
    recorder_holding_the_first_event: TestClient,
    path: str,
) -> None:
    reply = recorder_holding_the_first_event.get(path)

    assert reply.status_code == HTTPStatus.NOT_FOUND


def test_a_malformed_handle_is_refused_with_422(recorder: TestClient) -> None:
    reply = _post(recorder, _event(1), route=RECORDER_EVENTS_ROUTE.format(handle="not-a-handle"))

    assert reply.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.fixture
def recorder_built_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The recorder as uvicorn builds it, from settings with a small body limit; logs under ``tmp_path``."""
    settings = LocArenaSettings(gateway=GatewaySettings(max_request_bytes=SMALL_BODY_LIMIT))
    monkeypatch.setenv(SETTINGS_ENVIRONMENT_VARIABLE, settings.model_dump_json())
    monkeypatch.setattr(f"{build_recorder_app.__module__}.SEALED_MOUNT_PATH", tmp_path)
    return TestClient(build_recorder_app())


def test_the_built_recorder_applies_the_request_limit_of_its_settings(
    recorder_built_from_the_environment: TestClient,
) -> None:
    reply = recorder_built_from_the_environment.post(MODEL_CALLS, json=OVERSIZED_RECORD.model_dump())

    assert reply.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
