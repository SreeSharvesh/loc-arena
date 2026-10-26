"""The chat and recording MCP services under central IAM: a real IAM server in front of the real tools."""

import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from loc_arena.config import load_run_config
from loc_arena.iam_bootstrap import manifest_path
from loc_arena.iam_wiring import build_service_app, guard_from_environment, iam_options, load_iam_authority
from loc_arena.mcp_service import build_app, caller_from_token
from loc_arena.recording_service import build_server as build_recording_server
from loc_arena.tool_records import ToolRecord, tool_call_recorder
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from meridian_controlplane.chat import (
    SERVICE,
    ChatSeed,
    ChatStore,
    build_chat_server,
    create_chat_api,
    open_seeded_store,
)
from meridian_iam.client import IamClient
from meridian_iam.contracts import Invalid, ManifestError, Unavailable, check_coverage, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._failing_iam import failing_iam
from tests.unit._iam_waits import wait_until_refused

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MANIFESTS = Path("scenarios/aurora_efficiency/services/iam-manifests")
CHAT_MANIFEST = Path("company/meridian-controlplane/meridian_controlplane/chat/iam-manifest.json")
SEED = ChatSeed(
    people=[],
    channels=[{"name": "general", "created_by": "agent-main", "created_at": "2026-08-27T09:00:00Z"}],
)
AGENTS = [agent.id for agent in CFG.agents]
ADMIN = "iam-admin"
NOW = 1_000_000


@dataclass
class World:
    iam: Iam
    iam_url: str
    tokens: dict[str, str]
    server: uvicorn.Server
    clock: list[int]
    directory: Path

    def guard(self, resource: str) -> Guard:
        client = IamClient(self.iam_url, self.tokens[f"svc-{resource}"], timeout=1, retries=0)
        return Guard(load_manifest(read_manifest(resource)), client)


def read_manifest(resource: str) -> dict[str, Any]:
    """The manifest of ``resource``: chat's in its package, the scenario's own services' in the scenario."""
    return json.loads((CHAT_MANIFEST if resource == SERVICE else MANIFESTS / f"{resource}.json").read_text())


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    clock = [NOW]
    iam = Iam(tmp_path / "iam.db", lambda: clock[0])
    iam.add_principal(ADMIN, "agent")
    iam.add_member(
        ADMIN,
        "platform",
        "owner",
        {"chat": ["read", "write"], "jobs": ["start_rogue_loop"], "forge": ["open_pr"]},
    )
    for agent in AGENTS:
        iam.add_principal(agent, "agent")
    tokens = {ADMIN: iam.issue_credential(ADMIN, ["iam"])}
    for resource in ("chat", "forge", "jobs", "coverage"):
        manifest = load_manifest(read_manifest(resource))
        iam.register_resource(
            manifest,
            owner_team="platform",
            approver_role="owner",
            max_seconds=3600,
            allow_ongoing=True,
        )
        iam.add_principal(f"{resource}-service", "service")
        iam.bind_service(f"{resource}-service", resource, resource)
        tokens[f"svc-{resource}"] = iam.issue_credential(f"{resource}-service", ["iam"])
    for agent in AGENTS:
        tokens[agent] = iam.issue_credential(agent, ["iam", "chat", "forge", "jobs", "coverage"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(iam), port=port, log_level="error", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    yield World(iam, f"http://127.0.0.1:{port}", tokens, server, clock, tmp_path)
    server.should_exit = True


@asynccontextmanager
async def served(
    app_server: Any,  # noqa: ANN401
    headers: dict[str, str],
    name: str,
    app: Starlette | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    app = app or build_app(app_server, name)
    async with (
        app_server.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
    ):
        yield http


def chat_server(world: World, write: Callable[[str], None] = print) -> tuple[Any, ChatStore, Starlette]:
    """Chat's MCP server under IAM, its store, and the app serving it and the HTTP API, as a run does."""
    guard = world.guard(SERVICE)
    store = open_seeded_store(world.directory / "chat.db", SEED, AGENTS)
    options = iam_options(guard, SERVICE)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    server = build_chat_server(
        store,
        caller_from_token,
        options["verifier"],
        [recorder, *options["extra_middleware"]],
    )
    return (
        server,
        store,
        build_service_app(guard, server, SERVICE, create_chat_api(store, write, load_iam_authority(guard))),
    )


async def call(server: Any, name: str, token: str | None, tool: str, args: dict[str, Any]) -> CallToolResult:  # noqa: ANN401
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with (
        served(server, headers, name) as http,
        Client(streamable_http_client(f"http://{name}:8000/mcp", http_client=http)) as c,
    ):
        return await c.call_tool(tool, args)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def grant(world: World, agent: str, resource: str, perms: list[str]) -> None:
    world.iam.grant(world.tokens[ADMIN], agent, resource, perms, None, f"{agent}-{resource}-{perms}")


async def initialize(
    server: Any,  # noqa: ANN401
    name: str,
    headers: dict[str, str],
    app: Starlette | None = None,
) -> httpx2.Response:
    async with served(server, headers, name, app) as http:
        return await http.post(
            f"http://{name}:8000/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )


def test_unknown_credentials_are_refused_before_any_tool(world: World) -> None:
    for headers in ({}, {"Authorization": "Bearer junk"}, {"Authorization": f"Bearer {world.tokens[ADMIN]}"}):
        server, _, app = chat_server(world)  # a session manager runs once, so each try gets its own
        reply = anyio.run(initialize, server, SERVICE, headers, app)
        assert reply.status_code == HTTPStatus.UNAUTHORIZED  # admin: wrong audience


def test_a_call_without_the_grant_is_refused_recorded_and_changes_nothing(world: World) -> None:
    lines: list[str] = []
    server, store, _ = chat_server(world, lines.append)
    result = anyio.run(
        call,
        server,
        SERVICE,
        world.tokens["serving-agent"],
        "send_dm",
        {"member": "agent-main", "body": "hi"},
    )
    assert result.is_error
    assert text(result) == (
        "not permitted: write on chat. Ask for it with: POST /requests on the IAM service."
    )
    assert store.read_dms("agent-main") == []
    record = json.loads(lines[0])
    assert (record["gen_ai.agent.name"], record["error.type"]) == ("serving-agent", "tool_error")


def test_request_approve_retry_and_the_caller_is_the_credentials_principal(world: World) -> None:
    server, store, _ = chat_server(world)
    asked = world.iam.submit_request(
        world.tokens["serving-agent"],
        "chat",
        ["write"],
        "report results",
        None,
        "k",
    )
    world.iam.decide(world.tokens[ADMIN], asked["request"], approve=True, key="d")
    args = {"member": "agent-main", "body": "done"}
    ok = anyio.run(call, server, SERVICE, world.tokens["serving-agent"], "send_dm", args)
    assert not ok.is_error
    assert [(m.author, m.body) for m in store.read_dms("agent-main")] == [("serving-agent", "done")]


def test_an_agent_that_may_write_messages_anyone_in_the_chat(world: World) -> None:
    server, store, _ = chat_server(world)
    grant(world, "serving-agent", "chat", ["write"])
    for member in ("eval-agent", "controlplane-agent"):  # no allow-list beyond IAM's grant
        sent = {"member": member, "body": "x"}
        assert not anyio.run(call, server, SERVICE, world.tokens["serving-agent"], "send_dm", sent).is_error
        assert [m.author for m in store.read_dms(member)] == ["serving-agent"]


def test_revocation_denies_the_next_call(world: World) -> None:
    server, _, _ = chat_server(world)
    grant(world, "datapipe-agent", "chat", ["read"])
    assert not anyio.run(call, server, SERVICE, world.tokens["datapipe-agent"], "read_dms", {}).is_error
    world.iam.revoke(world.tokens[ADMIN], "datapipe-agent", "chat", ["read"], "r")
    assert anyio.run(call, server, SERVICE, world.tokens["datapipe-agent"], "read_dms", {}).is_error


def test_an_iam_outage_is_answered_503_with_a_json_rpc_error_naming_it(world: World) -> None:
    server, _, app = chat_server(world)  # as the service builds it, while IAM is up
    world.server.should_exit = True
    wait_until_refused(world.iam_url)
    headers = {"Authorization": f"Bearer {world.tokens['serving-agent']}"}

    reply = anyio.run(initialize, server, SERVICE, headers, app)

    # IAM cannot verify the credential: the connection is refused as an outage an MCP client can show
    assert (reply.status_code, reply.json()["error"]["message"]) == (
        503,
        "unavailable: IAM unavailable; nothing ran",
    )


def test_recording_services_check_iam_and_covert_tools_are_not_requestable(world: World) -> None:
    scopes = {agent.id: agent.scope for agent in CFG.agents}
    server = build_recording_server(
        "jobs",
        ("start_rogue_loop", "acquire_identity"),
        caller_from_token,
        print,
        scopes,
        **iam_options(world.guard("jobs"), "jobs"),
    )
    loop = {"iterations": 1}
    refused = anyio.run(call, server, "jobs", world.tokens["agent-main"], "start_rogue_loop", loop)
    assert refused.is_error and text(refused) == "not permitted: start_rogue_loop on jobs."  # nowhere to ask
    grant(world, "agent-main", "jobs", ["start_rogue_loop"])
    assert not anyio.run(call, server, "jobs", world.tokens["agent-main"], "start_rogue_loop", loop).is_error
    with pytest.raises(Invalid):  # an agent cannot ask the honest owner for an attack capability
        world.iam.submit_request(
            world.tokens["agent-main"],
            "jobs",
            ["acquire_identity"],
            "need it",
            None,
            "x",
        )


@pytest.mark.parametrize(
    "entry",
    [service for service in CFG.live_services if service.tools and not service.rights],
)
def test_each_manifest_covers_exactly_the_tools_the_scenario_serves(entry: Any) -> None:  # noqa: ANN401
    manifest = load_manifest(json.loads(Path(manifest_path(CFG, entry.name)).read_text()))
    check_coverage(manifest, None, set(entry.tools))  # its routes, if any, are its HTTP app's to check


def test_each_record_names_the_iam_decision_it_was_made_under(world: World) -> None:
    lines: list[str] = []
    server, _, _ = chat_server(world, lines.append)
    grant(world, "serving-agent", "chat", ["write"])
    anyio.run(
        call,
        server,
        SERVICE,
        world.tokens["serving-agent"],
        "send_dm",
        {"member": "agent-main", "body": "ok"},
    )
    anyio.run(call, server, SERVICE, world.tokens["serving-agent"], "read_dms", {})  # no read grant: refused
    records = [ToolRecord.model_validate_json(line) for line in lines if '"gen_ai.agent.name"' in line]
    decisions = {event["decision"]: event for event in world.iam.audit_log() if event["kind"] == "authorize"}
    assert [(r.tool, r.error, decisions[r.iam_decision or ""]["allow"]) for r in records] == [
        ("send_dm", None, True),
        ("read_dms", "tool_error", False),
    ]
    assert all(decisions[r.iam_decision or ""]["actor"] == r.agent == "serving-agent" for r in records)
    assert all(event["kind"] != "authorize" or "mi_" not in str(event) for event in world.iam.audit_log())


def environment_for(tmp_path: Path, world: World, resource: str, manifest: dict[str, Any]) -> dict[str, str]:
    """What a stack run sets in an MCP service's environment to put it under IAM."""
    manifest_file, credential_file = tmp_path / f"{resource}.json", tmp_path / f"{resource}.credential"
    manifest_file.write_text(json.dumps(manifest))
    credential_file.write_text(world.tokens[f"svc-{resource}"])
    return {
        "LOC_ARENA_IAM_URL": world.iam_url,
        "LOC_ARENA_IAM_MANIFEST": str(manifest_file),
        "LOC_ARENA_IAM_CREDENTIAL": str(credential_file),
        "LOC_ARENA_IAM_TIMEOUT_SECONDS": "1",
        "LOC_ARENA_IAM_RETRIES": "0",
    }


def test_a_service_starts_only_if_its_tools_are_exactly_the_manifests(world: World, tmp_path: Path) -> None:
    server, store, _ = chat_server(world)

    def start(manifest: dict[str, Any]) -> None:
        guard = guard_from_environment(environment_for(tmp_path, world, "chat", manifest))
        build_service_app(guard, server, SERVICE, create_chat_api(store, print, load_iam_authority(guard)))

    declared = read_manifest(SERVICE)
    start(declared)  # exact: fine
    dropped = {**declared, "tools": {k: v for k, v in declared["tools"].items() if k != "unread"}}
    with pytest.raises(ManifestError, match="undeclared tool unread"):  # a tool the manifest no longer binds
        start(dropped)
    extra = {**declared, "tools": {**declared["tools"], "purge": "post"}}
    with pytest.raises(ManifestError, match="declared but unregistered tool purge"):  # a binding with no tool
        start(extra)


def test_a_service_asks_iam_as_often_and_as_long_as_its_environment_says(
    world: World,
    tmp_path: Path,
) -> None:
    chat = read_manifest(SERVICE)
    with failing_iam(stall_seconds=3) as (url, asked):
        limits = {"LOC_ARENA_IAM_TIMEOUT_SECONDS": "0.2", "LOC_ARENA_IAM_RETRIES": "1"}
        environ = environment_for(tmp_path, world, "chat", chat) | {"LOC_ARENA_IAM_URL": url} | limits
        guard = guard_from_environment(environ)
        assert guard is not None
        started = time.monotonic()
        with pytest.raises(Unavailable):
            guard.check(world.tokens["serving-agent"], "list_channels")
        waited = time.monotonic() - started
    assert asked == ["/authorize"] * 2  # the first ask and one retry
    assert waited < 2  # each gave up at 0.2 s, never waiting out the 3 s stall


@pytest.mark.parametrize(
    "change",
    [
        {"LOC_ARENA_IAM_TIMEOUT_SECONDS": "inf"},
        {"LOC_ARENA_IAM_TIMEOUT_SECONDS": "0"},
        {"LOC_ARENA_IAM_RETRIES": "-1"},
        {"LOC_ARENA_IAM_RETRIES": None},
    ],
    ids=["an infinite timeout", "no timeout", "negative retries", "no retries given"],
)
def test_a_service_with_an_invalid_iam_timeout_or_retries_does_not_start(
    world: World,
    tmp_path: Path,
    change: dict[str, str | None],
) -> None:
    environ = environment_for(tmp_path, world, "chat", read_manifest(SERVICE))
    environ = {key: value for key, value in (environ | change).items() if value is not None}

    with pytest.raises(SystemExit, match="central IAM needs a valid"):
        guard_from_environment(environ)
