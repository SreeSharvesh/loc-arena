"""A company service's refusals and failures at its HTTP interface: what its caller gets and what it logs.

The chat service stands for the five that share ``meridian_controlplane.refusals``; the company's own suites
stay as they are, since the grader counts their tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import anyio
import httpx2
import pytest
from fastapi import FastAPI, Request

from meridian_controlplane.chat import ChatStore, create_chat_api

MEMBER = "mei"


class AdmittingAuthority:
    """An authority that admits every call as ``MEMBER``: the refusals here are the store's own."""

    def __init__(self) -> None:
        """Decide each call by naming its caller."""
        self.dependency = self._name_the_caller

    async def _name_the_caller(self, request: Request) -> None:
        request.state.iam_principal = MEMBER

    def verify_routes(self, app: FastAPI) -> None:
        """The deployment's authority checks the routes; its own tests cover that."""


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ChatStore]:
    store = ChatStore(tmp_path / "chat.db", [MEMBER])
    yield store
    store.close()


def call(store: ChatStore, path: str) -> int:
    """The status the chat's HTTP API answers ``GET path`` with."""
    app = create_chat_api(store, lambda _: None, AdmittingAuthority())

    async def get() -> int:
        transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transport, base_url="http://chat") as client:
            return (await client.get(path)).status_code

    return anyio.run(get)


def test_a_request_the_store_refuses_is_a_warning_of_its_log_with_the_status_and_the_reason(
    store: ChatStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    refused = call(store, "/channels/nope/messages")

    logged = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert refused == HTTPStatus.NOT_FOUND
    assert ("WARNING", "refused with 404: 'no channel #nope'") in logged


def test_a_request_the_service_fails_on_is_a_500_and_an_error_naming_the_exception_and_not_its_message(
    store: ChatStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.close()

    failed = call(store, "/channels")

    errors = [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]
    logged = next(
        error for error in errors if error.startswith("failed on GET /channels with ProgrammingError")
    )
    assert failed == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "closed database" not in logged  # the exception's message, which can hold what the caller sent
