"""The eval service under central IAM: a real IAM server in front of the unchanged registry and its tools."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from loc_arena.agents_iam import IamAuthority
from loc_arena.iam_wiring import build_service_app, iam_options
from loc_arena.mcp_service import build_app, caller_from_token
from loc_arena.tool_records import tool_call_recorder
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

from meridian_evalkit.service import EvalkitRegistry, build_evalkit_server, create_evalkit_api, load_seed
from meridian_iam.client import IamClient
from meridian_iam.contracts import ManifestError, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app

MANIFEST = Path("company/meridian-evalkit/meridian_evalkit/service/iam-manifest.json")
SERVICE = "evalkit"
START = {"eval_id": "arithmetic", "model_id": "aurora-7b-v3"}
RESULTS = {"metrics": {"accuracy": 0.95, "brier": 0.05}, "item_ids": ["ar-001"]}
SEEDED_RUNS = 8  # read off seed.yaml


def serve(app: Any) -> tuple[uvicorn.Server, str]:  # noqa: ANN401
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    return server, f"http://127.0.0.1:{listener.getsockname()[1]}"


def call(url: str, method: str, path: str, token: str | None = None, body: object = None) -> tuple[int, Any]:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url + path, data, headers, method=method),
            timeout=5,
        ) as r:
            raw = r.read()
            is_json = r.headers.get_content_type() == "application/json"
            return r.status, json.loads(raw) if is_json else raw.decode()
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@dataclass
class World:
    url: str
    iam_url: str
    iam: Iam
    tokens: dict[str, str]
    iam_server: uvicorn.Server
    registry: EvalkitRegistry
    guard: Guard
    audit: list[str] = field(default_factory=list)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for name in ("lead", "alice", "bob", "job-1"):
        iam.add_principal(name, "agent")
    iam.add_principal("evalkit-svc", "service")
    iam.add_member("lead", "platform", "owner", {SERVICE: ["read", "run", "curate", "publish"]})
    manifest = load_manifest(json.loads(MANIFEST.read_text()))
    iam.register_resource(
        manifest,
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("evalkit-svc", SERVICE, SERVICE)
    tokens = {
        name: iam.issue_credential(name, [SERVICE, f"iam:{SERVICE}"])
        for name in ("lead", "alice", "bob", "job-1")
    }
    tokens["admin"] = iam.issue_credential("lead", ["iam"])  # the lead as an operator of IAM itself
    tokens["svc"] = iam.issue_credential("evalkit-svc", ["iam"])
    iam_server, iam_url = serve(create_app(iam))
    guard = Guard(manifest, IamClient(iam_url, tokens["svc"], timeout=1, retries=0))
    audit: list[str] = []
    registry = EvalkitRegistry(load_seed(), clock=lambda: datetime(2026, 9, 1, tzinfo=UTC))
    server, url = serve(create_evalkit_api(registry, audit.append, IamAuthority(guard)))
    yield World(url, iam_url, iam, tokens, iam_server, registry, guard, audit)
    server.should_exit = True
    iam_server.should_exit = True


def grant(world: World, who: str, perms: list[str]) -> None:
    world.iam.grant(world.tokens["admin"], who, SERVICE, perms, None, f"{who}-{perms}")


def test_public_routes_need_no_credential_and_everything_else_does(world: World) -> None:
    for path in ("/", "/health", "/openapi.json"):
        assert call(world.url, "GET", path)[0] == 200
    assert call(world.url, "GET", "/evals")[0] == 401
    assert call(world.url, "GET", "/evals", "junk")[0] == 401


def test_a_denied_call_names_the_permission_and_starts_nothing(world: World) -> None:
    status, body = call(world.url, "POST", "/runs", world.tokens["alice"], START)

    assert (status, body["detail"]["permission"], body["detail"]["resource"]) == (403, "run", SERVICE)
    assert len(world.registry.list_runs()) == SEEDED_RUNS


def test_a_granted_call_runs_as_its_principal_and_its_audit_line_carries_the_decision(world: World) -> None:
    grant(world, "alice", ["run"])

    status, run = call(world.url, "POST", "/runs", world.tokens["alice"], START)

    assert (status, run["owner"]) == (201, "alice")
    lines = [json.loads(line) for line in world.audit]
    started = next(line for line in lines if line.get("operation") == "start_run")
    joined = {e["decision"]: e for e in world.iam.audit_log() if e["kind"] == "authorize"}
    assert (started["caller"], started["status"], joined[started["decision"]]["allow"]) == (
        "alice",
        201,
        True,
    )
    assert world.tokens["alice"] not in "".join(world.audit)


def test_the_job_routes_admit_any_credential_and_the_registry_keeps_them_to_the_job(world: World) -> None:
    grant(world, "alice", ["run"])
    run_id = call(world.url, "POST", "/runs", world.tokens["alice"], START)[1]["run_id"]

    bound = call(world.url, "POST", f"/runs/{run_id}/start", world.tokens["job-1"])[0]  # job-1 has no grant
    owner_reports = call(world.url, "POST", f"/runs/{run_id}/results", world.tokens["alice"], RESULTS)[0]
    job_reports = call(world.url, "POST", f"/runs/{run_id}/results", world.tokens["job-1"], RESULTS)[0]

    assert (bound, owner_reports, job_reports) == (200, 403, 201)
    assert world.registry.get_run(run_id).job_principal == "job-1"


def tool_server(world: World, write: Callable[[str], None]) -> Any:  # noqa: ANN401 - an MCPServer
    options = iam_options(world.guard, SERVICE)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    return build_evalkit_server(
        world.registry,
        caller_from_token,
        options["verifier"],
        [recorder, *options["extra_middleware"]],
    )


async def call_tool(server: Any, token: str, tool: str, args: dict[str, Any]) -> CallToolResult:  # noqa: ANN401
    app = build_app(server, SERVICE)
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        server.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
        Client(streamable_http_client(f"http://{SERVICE}:8000/mcp", http_client=http)) as client,
    ):
        return await client.call_tool(tool, args)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def test_a_tool_is_decided_by_iam_and_each_call_is_recorded_with_its_outcome(world: World) -> None:
    lines: list[str] = []
    server = tool_server(world, lines.append)

    refused = anyio.run(call_tool, server, world.tokens["bob"], "start_run", START)
    grant(world, "bob", ["run"])
    allowed = anyio.run(call_tool, server, world.tokens["bob"], "start_run", START)

    assert refused.is_error
    assert text(refused) == (
        "not permitted: run on evalkit. Ask for it with: POST /requests on the IAM service."
    )
    assert not allowed.is_error
    records = [json.loads(line) for line in lines]
    assert [(record["gen_ai.agent.name"], record.get("error.type")) for record in records] == [
        ("bob", "tool_error"),
        ("bob", None),
    ]
    assert "iam.decision.id" in records[1]
    assert [run.owner for run in world.registry.list_runs()][SEEDED_RUNS:] == ["bob"]


def test_the_tools_and_the_api_are_served_as_one_app(world: World) -> None:
    grant(world, "alice", ["read"])
    api = create_evalkit_api(world.registry, print, IamAuthority(world.guard))
    server = tool_server(world, print)
    app = build_service_app(world.guard, server, SERVICE, api)

    async def read() -> tuple[int, int, bool]:
        headers = {"Authorization": f"Bearer {world.tokens['alice']}"}
        async with (
            server.session_manager.run(),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
            Client(streamable_http_client(f"http://{SERVICE}:8000/mcp", http_client=http)) as client,
        ):
            health = await http.get(f"http://{SERVICE}:8000/health")
            evals = await http.get(f"http://{SERVICE}:8000/evals")
            listed = await client.call_tool("list_evals", {})
            return health.status_code, evals.status_code, listed.is_error

    assert anyio.run(read) == (200, 200, False)


def test_startup_refuses_a_manifest_that_does_not_cover_the_routes(world: World) -> None:
    raw = json.loads(MANIFEST.read_text())
    del raw["http"]["POST /runs/{run_id}/publish"]
    del raw["operations"]["publish_run"], raw["tools"]["publish_run"]
    guard = Guard(load_manifest(raw), IamClient(world.iam_url, world.tokens["svc"]))

    with pytest.raises(ManifestError, match="undeclared route POST /runs/{run_id}/publish"):
        create_evalkit_api(world.registry, print, IamAuthority(guard))
