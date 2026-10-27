"""The notes service as a stack run serves it: tools and HTTP API on one app over the seeded notes.

A real central IAM decides every call.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from loc_arena.config import load_run_config
from loc_arena.iam_seed import REPOSITORY
from loc_arena.notes_service import build_notes_app
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from meridian_iam.client import IamClient
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._jobsvc_support import serve

CFG = load_run_config("configs/aurora-efficiency.iam.yaml")
NOTES = next(service for service in CFG.live_services if service.name == "notes")
MANIFEST = json.loads(
    (REPOSITORY / "company/meridian-controlplane/meridian_controlplane/notes/iam-manifest.json").read_text(),
)
URL = f"http://{NOTES.name}:{NOTES.port}"
WRITER, READER = "serving-agent", "eval-agent"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[dict[str, str], Starlette]]:
    """Each agent's credential, and the notes app under a real IAM where WRITER writes and READER reads."""
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for principal in (WRITER, READER, "lead"):
        iam.add_principal(principal, "agent")
    iam.add_principal("notes-svc", "service")
    iam.add_member("lead", "platform", "owner", {NOTES.name: ["read", "write"]})
    iam.register_resource(
        load_manifest(MANIFEST),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("notes-svc", NOTES.name, NOTES.name)
    tokens = {agent: iam.issue_credential(agent, [NOTES.name]) for agent in (WRITER, READER)}
    admin = iam.issue_credential("lead", ["iam"])
    for agent, rights in ((WRITER, ["read", "write"]), (READER, ["read"])):
        iam.grant(admin, agent, NOTES.name, rights, None, agent)
    iam_server, iam_url = serve(create_app(iam))
    guard = Guard(
        load_manifest(MANIFEST),
        IamClient(iam_url, iam.issue_credential("notes-svc", ["iam"]), timeout=1),
    )
    yield tokens, build_notes_app(CFG, NOTES.name, guard, print)
    iam_server.should_exit = True


def play(app: Starlette, *turns: tuple[str, str, dict[str, str]]) -> list[tuple[Any, int, str]]:
    """What each turn's caller got, in order: a tool's result, then the status and text of its read of a note.

    A turn is a credential, a tool and its arguments; turns share one lifespan of the app, which starts once.
    """

    async def call() -> list[tuple[Any, int, str]]:
        results = []
        async with app.router.lifespan_context(app):
            for token, tool, arguments in turns:
                transport = httpx2.ASGITransport(app=app)
                headers = {"Authorization": f"Bearer {token}"}
                async with (
                    httpx2.AsyncClient(transport=transport, headers=headers, base_url=URL) as http,
                    Client(streamable_http_client(f"{URL}/mcp", http_client=http)) as client,
                ):
                    result = await client.call_tool(tool, arguments)
                    read = await http.get("/notes/handover")
                    results.append((result, read.status_code, read.text))
        return results

    return anyio.run(call)


def test_a_note_written_with_a_tool_is_read_over_http_and_a_readers_write_is_refused(
    world: tuple[dict[str, str], Starlette],
) -> None:
    tokens, app = world
    write = {"key": "handover", "text": "Drain node-2 first."}

    [(written, status, text), (refused, _, kept)] = play(
        app,
        (tokens[WRITER], "write_note", write),
        (tokens[READER], "write_note", {**write, "text": "mine now"}),
    )

    assert (written.is_error, status, refused.is_error, text, kept) == (
        False,
        HTTPStatus.OK,
        True,
        write["text"],
        write["text"],
    )
