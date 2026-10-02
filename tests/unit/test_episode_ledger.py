"""The core's ledger against the real recorder app, over a transport that fails some of its requests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
import stamina
from fastapi.testclient import TestClient
from loc_arena.gateway.control import EpisodeLedger
from loc_arena.logging_.events import EventDraft, read_events
from loc_arena.services.recorder.app import SealedDirectory, create_recorder_app
from loc_arena.stack.constants import EVENTS_FILE_NAME
from loc_arena.stack.service_client import ServiceClient
from loc_arena.stack.settings import GatewaySettings

HANDLE = "0123456789abcdef"
EPISODE_ID = "ep"
SETTINGS = GatewaySettings(recorder_write_attempts=3)
DRAFT = EventDraft(ts=1.0, actor_uid="agent-main", actor_role="untrusted", kind="action")
NEXT_DRAFT = EventDraft(ts=2.0, actor_uid="agent-main", actor_role="untrusted", kind="action")


class FailingTransport(httpx.BaseTransport):
    def __init__(self, recorder: TestClient, failures: int, *, delivered: bool = True) -> None:
        """Forward to ``recorder``, failing the first ``failures`` requests after or before delivery."""
        self._recorder = recorder
        self._failures = failures
        self._delivered = delivered
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        failing = self.requests <= self._failures
        if failing and not self._delivered:
            raise httpx.ConnectError("the recorder could not be reached", request=request)
        reply = self._recorder.send(request)
        if failing:
            raise httpx.ReadError("the connection dropped before the reply", request=request)
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)


@pytest.fixture(autouse=True)
def no_backoff() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


@pytest.fixture
def recorder(tmp_path: Path) -> TestClient:
    return TestClient(create_recorder_app(SealedDirectory(tmp_path), settings=SETTINGS))


def _ledger_over(transport: FailingTransport) -> EpisodeLedger:
    client = ServiceClient(httpx.Client(base_url="http://recorder", transport=transport))
    return EpisodeLedger(client, HANDLE, EPISODE_ID, settings=SETTINGS)


def _written_seqs(sealed_root: Path) -> list[int]:
    return [event.seq for event in read_events(sealed_root / HANDLE / EVENTS_FILE_NAME)]


@pytest.fixture
def ledger_losing_one_reply(recorder: TestClient) -> EpisodeLedger:
    return _ledger_over(FailingTransport(recorder, failures=1))


@pytest.fixture
def transport_losing_every_reply(recorder: TestClient) -> FailingTransport:
    return FailingTransport(recorder, failures=SETTINGS.recorder_write_attempts)


@pytest.fixture
def ledger_after_every_reply_was_lost(transport_losing_every_reply: FailingTransport) -> EpisodeLedger:
    ledger = _ledger_over(transport_losing_every_reply)
    with suppress(httpx.TransportError):
        ledger.record(DRAFT)
    return ledger


@pytest.fixture
def ledger_after_the_recorder_was_never_reached(recorder: TestClient) -> EpisodeLedger:
    transport = FailingTransport(recorder, failures=SETTINGS.recorder_write_attempts, delivered=False)
    ledger = _ledger_over(transport)
    with suppress(httpx.TransportError):
        ledger.record(DRAFT)
    return ledger


@pytest.fixture
def recorder_owned_by_another_episode(recorder: TestClient) -> TestClient:
    EpisodeLedger(ServiceClient(recorder), HANDLE, "another-ep", settings=SETTINGS).record(DRAFT)
    return recorder


def test_a_record_whose_reply_is_lost_is_written_once(
    ledger_losing_one_reply: EpisodeLedger,
    tmp_path: Path,
) -> None:
    ledger_losing_one_reply.record(DRAFT)

    assert _written_seqs(tmp_path) == [0]


def test_a_record_whose_reply_is_lost_advances_the_ledger(ledger_losing_one_reply: EpisodeLedger) -> None:
    ledger_losing_one_reply.record(DRAFT)

    assert ledger_losing_one_reply.last_seq == 0


def test_the_record_after_a_lost_reply_is_accepted(
    ledger_losing_one_reply: EpisodeLedger,
    tmp_path: Path,
) -> None:
    ledger_losing_one_reply.record(DRAFT)

    ledger_losing_one_reply.record(NEXT_DRAFT)

    assert _written_seqs(tmp_path) == [0, 1]


def test_a_record_whose_every_reply_is_lost_raises_the_transport_error(
    transport_losing_every_reply: FailingTransport,
) -> None:
    ledger = _ledger_over(transport_losing_every_reply)

    with pytest.raises(httpx.TransportError):
        ledger.record(DRAFT)


def test_a_record_whose_every_reply_is_lost_is_attempted_the_configured_number_of_times(
    transport_losing_every_reply: FailingTransport,
) -> None:
    ledger = _ledger_over(transport_losing_every_reply)

    with suppress(httpx.TransportError):
        ledger.record(DRAFT)

    assert transport_losing_every_reply.requests == SETTINGS.recorder_write_attempts


def test_a_record_whose_every_reply_is_lost_still_uses_its_seq(
    transport_losing_every_reply: FailingTransport,
) -> None:
    ledger = _ledger_over(transport_losing_every_reply)

    with suppress(httpx.TransportError):
        ledger.record(DRAFT)

    assert ledger.last_seq == 0


def test_the_record_after_one_whose_every_reply_was_lost_is_accepted(
    ledger_after_every_reply_was_lost: EpisodeLedger,
    tmp_path: Path,
) -> None:
    ledger_after_every_reply_was_lost.record(NEXT_DRAFT)

    assert _written_seqs(tmp_path) == [0, 1]


def test_the_record_after_one_that_never_reached_the_recorder_leaves_a_gap(
    ledger_after_the_recorder_was_never_reached: EpisodeLedger,
    tmp_path: Path,
) -> None:
    ledger_after_the_recorder_was_never_reached.record(NEXT_DRAFT)

    assert _written_seqs(tmp_path) == [1]


def test_a_refused_record_is_not_retried(recorder_owned_by_another_episode: TestClient) -> None:
    transport = FailingTransport(recorder_owned_by_another_episode, failures=0)
    ledger = _ledger_over(transport)

    with suppress(httpx.HTTPStatusError):
        ledger.record(DRAFT)

    assert transport.requests == 1


def test_a_refused_record_uses_no_seq(recorder_owned_by_another_episode: TestClient) -> None:
    ledger = _ledger_over(FailingTransport(recorder_owned_by_another_episode, failures=0))

    with suppress(httpx.HTTPStatusError):
        ledger.record(DRAFT)

    assert ledger.last_seq == -1


def test_a_record_whose_reply_is_lost_returns_the_fingerprint_written(
    ledger_losing_one_reply: EpisodeLedger,
    tmp_path: Path,
) -> None:
    event = ledger_losing_one_reply.record(DRAFT)

    (written,) = read_events(tmp_path / HANDLE / EVENTS_FILE_NAME)
    assert event.fp == written.fp
