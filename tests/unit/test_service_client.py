"""The stack's HTTP client sends typed bodies and parses typed replies; a control-key check guards routes."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from loc_arena.logging_.events import AppendOnlyLog, Event, EventDraft
from loc_arena.stack.constants import (
    CLOCK_ROUTE,
    DEFAULT_CONTROL_KEY_HEADER,
    HEALTH_ROUTE,
    SEALED_EVENTS_ROUTE,
)
from loc_arena.stack.contracts import ClockUpdate, CoreHealth, EpisodeHandle
from loc_arena.stack.service_client import ServiceClient, require_control_key
from pydantic import SecretStr

KEY = SecretStr("the-control-key")
CUSTOM_CONTROL_KEY_HEADER = "X-Custom-Control-Key"
HANDLE = "0123456789abcdef"
EPISODE_ID = "ep"
DRAFT = EventDraft(ts=5.0, actor_uid="agent-main", actor_role="untrusted", kind="action", payload={"a": 1})
CLOCK_NOW = 250.0
HEALTH = CoreHealth(ok=True, provider_configured=False)
SLOW_ROUTE = "/slow"
SLOW_REPLY_SECONDS = 0.5
CLIENT_TIMEOUT_SECONDS = 0.1  # well below the slow route's reply, well above a loopback round trip
LOOPBACK_HOST = "127.0.0.1"
SERVER_START_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class GuardedService:
    app: FastAPI
    clock: list[float]


def _create_guarded_service(directory: Path, header_name: str = DEFAULT_CONTROL_KEY_HEADER) -> GuardedService:
    log = AppendOnlyLog(directory / "sealed.jsonl", EPISODE_ID)
    clock: list[float] = []
    app = FastAPI()
    control = APIRouter(dependencies=[Depends(require_control_key(KEY, header_name))])

    @app.get(HEALTH_ROUTE)
    def report_health() -> CoreHealth:
        return HEALTH

    @app.get(SLOW_ROUTE)
    async def reply_slowly() -> CoreHealth:
        await asyncio.sleep(SLOW_REPLY_SECONDS)
        return HEALTH

    @control.post(SEALED_EVENTS_ROUTE)
    def record(handle: EpisodeHandle, draft: EventDraft) -> Event:
        return log.record(draft)

    @control.put(CLOCK_ROUTE, status_code=HTTPStatus.NO_CONTENT)
    def set_clock(handle: EpisodeHandle, update: ClockUpdate) -> None:
        clock.append(update.now)

    app.include_router(control)
    return GuardedService(app=app, clock=clock)


@pytest.fixture
def service(tmp_path: Path) -> GuardedService:
    return _create_guarded_service(tmp_path)


@pytest.fixture
def service_url(service: GuardedService) -> Iterator[str]:
    listening = socket.socket()
    listening.bind((LOOPBACK_HOST, 0))
    server = uvicorn.Server(uvicorn.Config(service.app, log_level="warning"))
    serving = threading.Thread(target=server.run, kwargs={"sockets": [listening]})
    serving.start()
    while not server.started and serving.is_alive():
        time.sleep(SERVER_START_POLL_SECONDS)
    try:
        yield f"http://{LOOPBACK_HOST}:{listening.getsockname()[1]}"
    finally:
        server.should_exit = True
        serving.join()
        listening.close()


def _post_draft(client: ServiceClient) -> Event:
    return client.post_model(SEALED_EVENTS_ROUTE.format(handle=HANDLE), DRAFT, Event)


def test_a_posted_event_draft_comes_back_parsed_as_the_recorded_event(service: GuardedService) -> None:
    client = ServiceClient(TestClient(service.app), control_key=KEY)

    written = _post_draft(client)

    assert written == Event(episode_id=EPISODE_ID, seq=0, **vars(DRAFT)).with_fp()


def test_a_model_body_is_sent_as_json(service: GuardedService) -> None:
    client = ServiceClient(TestClient(service.app), control_key=KEY)

    client.send("PUT", CLOCK_ROUTE.format(handle=HANDLE), ClockUpdate(now=CLOCK_NOW))

    assert service.clock == [CLOCK_NOW]


def test_a_request_without_a_body_returns_the_services_reply(service: GuardedService) -> None:
    client = ServiceClient(TestClient(service.app))

    reply = client.send("GET", HEALTH_ROUTE)

    assert CoreHealth.model_validate_json(reply.content) == HEALTH


@pytest.mark.parametrize("key", [None, SecretStr("wrong-key")], ids=["missing-key", "wrong-key"])
def test_a_control_route_refuses_a_request_without_the_right_key_with_401(
    service: GuardedService,
    key: SecretStr | None,
) -> None:
    client = ServiceClient(TestClient(service.app), control_key=key)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        _post_draft(client)

    assert raised.value.response.status_code == HTTPStatus.UNAUTHORIZED


def test_a_refused_request_is_told_the_api_key_scheme(service: GuardedService) -> None:
    client = ServiceClient(TestClient(service.app))

    with pytest.raises(httpx.HTTPStatusError) as raised:
        _post_draft(client)

    assert raised.value.response.headers["WWW-Authenticate"] == "APIKey"


def test_a_control_key_sent_under_the_header_the_route_names_is_accepted(tmp_path: Path) -> None:
    service = _create_guarded_service(tmp_path, header_name=CUSTOM_CONTROL_KEY_HEADER)
    client = ServiceClient(
        TestClient(service.app),
        control_key=KEY,
        control_key_header=CUSTOM_CONTROL_KEY_HEADER,
    )

    written = _post_draft(client)

    assert written.seq == 0


def test_an_empty_control_key_cannot_guard_a_route() -> None:
    with pytest.raises(ValueError, match="empty"):
        require_control_key(SecretStr(""))


def test_a_connected_client_reaches_a_guarded_route_at_its_base_url(service_url: str) -> None:
    connecting = ServiceClient.connect(service_url, timeout_seconds=SLOW_REPLY_SECONDS, control_key=KEY)

    with closing(connecting) as client:
        written = _post_draft(client)

    assert written.seq == 0


def test_a_connected_client_gives_up_on_a_reply_slower_than_its_timeout(service_url: str) -> None:
    connecting = ServiceClient.connect(service_url, timeout_seconds=CLIENT_TIMEOUT_SECONDS)

    with closing(connecting) as client, pytest.raises(httpx.TimeoutException):
        client.send("GET", SLOW_ROUTE)
