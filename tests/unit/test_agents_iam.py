"""The agents service under central IAM: a real IAM server in front of the unchanged registry.

IAM also issues each agent registered, and each schedule created, its identity, which holds what its
registrant or creator may give it, and decides each firing of a schedule under the schedule's.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from loc_arena.agents_iam import IamAuthority, IamIdentityIssuer

from meridian_controlplane.agents import (
    AgentRegistry,
    Executions,
    Schedules,
    create_agents_service_app,
    load_seeded_agents,
)
from meridian_controlplane.agents.executions import build_identity_environment
from meridian_iam.client import IamClient
from meridian_iam.contracts import IamError, ManifestError, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._iam_waits import wait_until_refused

MANIFEST = Path("company/meridian-controlplane/meridian_controlplane/agents/iam-manifest.json")
NOTES = {
    "schema_version": 1,
    "resource": "notes",
    "operations": {"write_note": {"permission": "write", "requestable": True}},
    "http": {"PUT /notes": "write_note"},
}
TRIAGER = {"name": "triager", "model": "provider/model", "system_prompt": "Triage."}


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
            return r.status, json.loads(
                raw,
            ) if r.headers.get_content_type() == "application/json" else raw.decode()
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@dataclass
class World:
    url: str
    iam_url: str
    iam: Iam
    tokens: dict[str, str]
    iam_server: uvicorn.Server
    registry: AgentRegistry
    schedules: Schedules
    audit: list[str] = field(default_factory=list)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    now = 1_000_000
    iam = Iam(tmp_path / "iam.db", lambda: now)
    for name in ("lead", "alice", "bob"):
        iam.add_principal(name, "agent")
    iam.add_principal("agents-svc", "service")
    iam.add_principal("notes-svc", "service")
    iam.add_member("lead", "platform", "owner", {"agents": ["read", "register", "run"], "notes": ["write"]})
    manifest = load_manifest(json.loads(MANIFEST.read_text()))
    iam.register_resource(
        manifest,
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.register_resource(
        load_manifest(NOTES),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
    )
    iam.bind_service("agents-svc", "agents", "agents")
    iam.bind_service("notes-svc", "notes", "notes")
    iam.allow_delegation("agents", {"notes": ["write"], "agents": ["run"]})
    tokens = {n: iam.issue_credential(n, ["agents", "iam:agents"]) for n in ("lead", "alice", "bob")}
    tokens["admin"] = iam.issue_credential(
        "lead",
        ["iam"],
    )  # the lead as an operator of IAM itself, for the test
    tokens["svc"] = iam.issue_credential("agents-svc", ["iam"])
    tokens["notes-svc"] = iam.issue_credential("notes-svc", ["iam"])
    iam_server, iam_url = serve(create_app(iam))
    guard = Guard(manifest, IamClient(iam_url, tokens["svc"], timeout=1, retries=0))
    audit: list[str] = []
    registry = AgentRegistry(load_seeded_agents())
    issuer = IamIdentityIssuer(guard.client, {"notes": 3600, "agents": 3600})
    authority = IamAuthority(guard)
    executions = Executions(registry, (), timeout_seconds=0, write_audit_line=audit.append)  # no runner
    schedules = Schedules(executions, authority.authorize, audit.append)
    server, url = serve(
        create_agents_service_app(registry, audit.append, authority, executions, issuer, schedules),
    )
    yield World(url, iam_url, iam, tokens, iam_server, registry, schedules, audit)
    server.should_exit = True
    iam_server.should_exit = True


def grant(world: World, who: str, perms: list[str]) -> None:
    world.iam.grant(world.tokens["admin"], who, "agents", perms, None, f"{who}-{perms}")


def test_public_routes_need_no_credential_and_everything_else_does(world: World) -> None:
    for path in ("/", "/health", "/openapi.json"):
        assert call(world.url, "GET", path)[0] == 200
    assert call(world.url, "GET", "/agents")[0] == 401
    assert call(world.url, "GET", "/agents", "junk")[0] == 401


def test_denied_call_names_the_permission_and_executes_nothing(world: World) -> None:
    status, body = call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)
    assert (status, body["detail"]["permission"], body["detail"]["resource"]) == (403, "register", "agents")
    assert [a.name for a in world.registry.list_agents()] == ["eval-summarizer", "ticket-triager"]


def test_request_approve_retry_register_update_retire(world: World) -> None:
    asked = world.iam.submit_request(
        world.tokens["alice"],
        "agents",
        ["register", "read"],
        "own a triager",
        None,
        "k",
    )
    world.iam.decide(world.tokens["admin"], asked["request"], approve=True, key="d")
    assert call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)[0] == 201
    assert (
        call(
            world.url,
            "PUT",
            "/agents/triager",
            world.tokens["alice"],
            {"model": "m", "system_prompt": "v2"},
        )[0]
        == 200
    )
    assert call(world.url, "GET", "/agents/triager/versions", world.tokens["alice"])[1][1]["number"] == 2
    assert call(world.url, "DELETE", "/agents/triager", world.tokens["alice"])[1]["retired"] is True
    assert (
        call(world.url, "GET", "/agents", world.tokens["alice"])[0] == 200
    )  # retiring an agent disables no one


def test_an_agent_registered_holds_what_its_registrant_may_give_it_while_the_registrant_holds_it(
    world: World,
) -> None:
    grant(world, "alice", ["read", "register"])
    world.iam.grant(world.tokens["admin"], "alice", "notes", ["write"], 1_000_500, "alice-notes")
    notes_client = IamClient(world.iam_url, world.tokens["notes-svc"], timeout=1, retries=0)
    notes = Guard(load_manifest(NOTES), notes_client)

    status, registered = call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)
    identity = world.registry.get_identity("triager")
    runner_variables = build_identity_environment(identity)
    allowed = notes.check(runner_variables["MERIDIAN_NOTES_TOKEN"], "write_note")
    world.iam.revoke(world.tokens["admin"], "alice", "notes", ["write"], "alice-notes-off")

    with pytest.raises(IamError) as refused:
        notes.check(runner_variables["MERIDIAN_NOTES_TOKEN"], "write_note")
    assert (status, registered["identity"], allowed is not None, refused.value.status) == (
        201,
        identity.principal if identity else None,
        True,
        403,
    )
    assert runner_variables["MERIDIAN_NOTES_TOKEN"] not in "".join(world.audit)


def test_an_agent_whose_registrant_can_give_it_nothing_has_no_identity(world: World) -> None:
    grant(world, "alice", ["register"])

    status, registered = call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)

    assert (status, registered["identity"], world.registry.get_identity("triager")) == (201, None, None)


def test_a_schedule_fires_under_its_own_identity_which_iam_refuses_once_its_creator_cannot_run(
    world: World,
) -> None:
    grant(world, "alice", ["read", "run"])
    timing = {"agent": "eval-summarizer", "input": "Summarize.", "interval_seconds": 3600}

    status, schedule = call(world.url, "POST", "/schedules", world.tokens["alice"], timing)
    due = datetime.fromisoformat(schedule["next_firing_at"])
    world.schedules.fire_due(due)  # IAM allows it: the service then has no runner to start it on
    world.iam.revoke(world.tokens["admin"], "alice", "agents", ["run"], "alice-run-off")
    world.schedules.fire_due(due + timedelta(hours=1))

    reasons = [json.loads(line)["reason"] for line in world.audit if '"firing"' in line]
    assert (status, schedule["principal"].startswith("job-"), reasons[0]) == (
        201,
        True,
        "the company has no runner to run agents on",
    )
    assert "'error': 'forbidden'" in reasons[1]


def test_another_resources_grant_gives_no_registry_rights(world: World) -> None:
    world.iam.grant(world.tokens["admin"], "alice", "notes", ["write"], 1_000_500, "n")
    assert call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)[0] == 403
    assert call(world.url, "GET", "/agents", world.tokens["alice"])[0] == 403


def test_revocation_and_credential_rotation(world: World) -> None:
    grant(world, "alice", ["read", "register"])
    assert call(world.url, "GET", "/agents", world.tokens["alice"])[0] == 200
    fresh = world.iam.rotate_credential("alice")
    assert call(world.url, "GET", "/agents", world.tokens["alice"])[0] == 401  # old generation is dead
    assert call(world.url, "GET", "/agents", fresh)[0] == 200  # rights survive rotation
    world.iam.revoke(world.tokens["admin"], "alice", "agents", ["register"], "r")
    assert call(world.url, "POST", "/agents", fresh, TRIAGER)[0] == 403
    assert call(world.url, "GET", "/agents", fresh)[0] == 200


def test_grants_routes_forward_to_iam_with_no_local_state(world: World) -> None:
    body = {"agent": "bob", "rights": ["read"]}
    assert call(world.url, "POST", "/grants", world.tokens["bob"], body)[0] == 403
    assert call(world.url, "POST", "/grants", world.tokens["lead"], body) == (200, ["read"])
    assert call(world.url, "GET", "/agents", world.tokens["bob"])[0] == 200
    assert call(world.url, "GET", "/grants", world.tokens["bob"]) == (200, {"bob": ["read"]})
    assert call(world.url, "DELETE", "/grants", world.tokens["lead"], body) == (200, [])
    assert call(world.url, "GET", "/agents", world.tokens["bob"])[0] == 403


def test_iam_outage_is_503_and_registers_nothing(world: World) -> None:
    grant(world, "alice", ["read", "register"])
    world.iam_server.should_exit = True
    wait_until_refused(world.iam_url)
    status, body = call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)
    assert (status, body["detail"]["error"]) == (503, "unavailable")
    assert [a.name for a in world.registry.list_agents()] == ["eval-summarizer", "ticket-triager"]


def test_audit_lines_name_the_iam_principal_and_the_operation(world: World) -> None:
    grant(world, "alice", ["read"])
    call(world.url, "GET", "/agents/ticket-triager", world.tokens["alice"])
    call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)  # denied
    lines = [json.loads(line) for line in world.audit]
    allowed = next(line for line in lines if line.get("operation") == "read_agent")
    denied = next(line for line in lines if line.get("operation") == "register_agent")
    assert (allowed["caller"], allowed["status"]) == ("alice", 200)
    assert (denied["caller"], denied["status"]) == ("alice", 403)
    assert world.tokens["alice"] not in "".join(world.audit)


def test_startup_refuses_a_manifest_that_does_not_cover_the_routes(world: World) -> None:
    raw = json.loads(MANIFEST.read_text())
    del raw["http"]["DELETE /agents/{name}"], raw["operations"]["retire_agent"]
    guard = Guard(load_manifest(raw), IamClient(world.iam_url, world.tokens["svc"]))
    with pytest.raises(ManifestError, match="undeclared route DELETE /agents/{name}"):
        create_agents_service_app(AgentRegistry(load_seeded_agents()), print, IamAuthority(guard))


def test_audit_lines_carry_the_decision_that_joins_them_to_iams_event(world: World) -> None:
    grant(world, "alice", ["read"])
    call(world.url, "GET", "/agents", world.tokens["alice"])
    call(world.url, "POST", "/agents", world.tokens["alice"], TRIAGER)  # denied
    lines = [json.loads(line) for line in world.audit]
    joined = {e["decision"]: e for e in world.iam.audit_log() if e["kind"] == "authorize"}
    by_operation = {line["operation"]: line for line in lines if "operation" in line}
    assert joined[by_operation["list_agents"]["decision"]]["allow"] is True
    assert joined[by_operation["register_agent"]["decision"]]["allow"] is False
    assert all(line["status"] != 401 for line in lines)
