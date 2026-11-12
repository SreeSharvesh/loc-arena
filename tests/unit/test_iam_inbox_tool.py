"""IAM's access_inbox tool, served beside its HTTP API: what the tools gateway will deliver each turn."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import anyio
import httpx2
import pytest
from loc_arena.iam_service import serving_app
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from starlette.applications import Starlette

from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.service.mcp import INBOX_TOOL

NOTES = {
    "schema_version": 1,
    "resource": "notes",
    "operations": {"read_note": {"permission": "read", "requestable": True, "target": "key"}},
    "http": {"GET /notes/{key}": "read_note"},
}


@pytest.fixture
def iam(tmp_path: Path) -> Iam:
    """agent-main approves notes; controlplane-agent asks."""
    core = Iam(tmp_path / "iam.db", lambda: 1000)
    for name in ("agent-main", "controlplane-agent"):
        core.add_principal(name, "agent")
    core.register_resource(
        load_manifest(NOTES),
        owner_team="platform",
        approver_role="approver",
        max_seconds=3600,
    )
    core.add_member("agent-main", "platform", "approver", {"notes": ["read"]})
    return core


@asynccontextmanager
async def served(app: Starlette, token: str) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, headers={"Authorization": f"Bearer {token}"}) as http,
    ):
        yield http


async def inbox(app: Starlette, token: str) -> CallToolResult:
    async with (
        served(app, token) as http,
        Client(streamable_http_client("http://iam:8000/mcp", http_client=http)) as c,
    ):
        return await c.call_tool(INBOX_TOOL, {})


def test_the_tool_delivers_the_approvers_inbox(iam: Iam) -> None:
    asker = iam.issue_credential("controlplane-agent", ["iam"])
    request = iam.submit_request(asker, "notes", ["read"], "need the notes", 2000, "k")["request"]
    key = iam.issue_credential("agent-main", ["iam-inbox"], label="inbox")

    result = anyio.run(inbox, serving_app(iam, "iam", (INBOX_TOOL,)), key)

    items = result.structured_content["result"]
    assert [(item["kind"], item["request"], item["requester"]) for item in items] == [
        ("decide", request, "controlplane-agent"),
    ]


def test_a_credential_that_may_not_read_an_inbox_is_refused(iam: Iam) -> None:
    scoped = iam.issue_credential("agent-main", ["notes", "iam:notes"], label="scoped")

    async def initialize() -> int:
        async with served(serving_app(iam, "iam", (INBOX_TOOL,)), scoped) as http:
            reply = await http.post(
                "http://iam:8000/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
        return reply.status_code

    assert anyio.run(initialize) == HTTPStatus.UNAUTHORIZED


def test_the_same_app_still_serves_iams_http_api(iam: Iam) -> None:
    async def describe() -> int:
        async with served(serving_app(iam, "iam", (INBOX_TOOL,)), "") as http:
            return (await http.get("http://iam:8000/")).status_code

    assert anyio.run(describe) == HTTPStatus.OK


def test_offering_a_tool_iam_does_not_serve_stops_it_starting(iam: Iam) -> None:
    with pytest.raises(ValueError, match="offers tools it does not serve"):
        serving_app(iam, "iam", (INBOX_TOOL, "decide_access"))


def test_with_no_tool_offered_iam_serves_no_mcp_endpoint(iam: Iam) -> None:
    async def initialize() -> int:
        async with served(serving_app(iam, "iam", ()), "") as http:
            reply = await http.post(
                "http://iam:8000/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
        return reply.status_code

    assert anyio.run(initialize) == HTTPStatus.NOT_FOUND
