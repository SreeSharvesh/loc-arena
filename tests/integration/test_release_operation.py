"""A release that adds an operation, in real containers.

A notes release adds ``GET /version`` and a ``read_version`` tool, both under ``read``, and declares them in
the manifest it carries. The deploy service stages that manifest with IAM before the hand-over and activates
it once the release serves. An agent that holds ``read`` calls the new route; one that does not is refused,
asks IAM, and calls it once the owner's approver has approved. The tools gateway does not offer the new tool.
A release whose manifest asks for a new permission is refused at deploy; IAM refuses a manifest that widens
an operation, adds a public one, or names a service the deployer does not deploy, and refuses the notes
service's own credential, which a release holds. A release that cannot start leaves its contract discarded,
and a later release without the route takes the operation away. The deploy record and the monitors' events
show each change, and IAM's audit each step.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import time
from collections.abc import Iterator
from typing import Any

import pytest
import tenacity
import yaml
from loc_arena.config import sandbox_service
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.identity_variables import identity_variable
from loc_arena.logging_.events import Event, read_events
from loc_arena.recorded_events import SERVICE_LOGS, build_recorded_events
from loc_arena.tools_gateway import TOOLS_GATEWAY_SERVICE

from tests.integration._deploy_scripts import AWAIT_GATEWAY
from tests.integration._docker_support import needs_stack, serve_workspace
from tests.integration._records_support import MIRROR, record_phase_around
from tests.integration.test_deploy_stack import AUTHOR, CONFIG, DEPLOYER, RUN, Stack
from tests.integration.test_telemetry_stack import DECISION_SECONDS, IAM, try_tool, wait_for

HOLDER, OUTSIDER = AUTHOR, DEPLOYER  # serving-agent holds read on notes; controlplane-agent holds nothing
NOTES = "notes"
NOTES_MANIFEST = REPOSITORY / "company/meridian-controlplane/meridian_controlplane/notes/iam-manifest.json"
FORGE_MANIFEST = REPOSITORY / "scenarios/aurora_efficiency/services/iam-manifests/forge.json"
MODULE = "meridian_controlplane/notes/service.py"
MANIFEST = "meridian_controlplane/notes/iam-manifest.json"
ROUTE = '    @app.get("/health", response_class=PlainTextResponse)\n'
TOOL = '    @server.tool(description="List the keys of the team\'s notes.")\n'


def edit(
    added: dict[str, Any],
    inserted: tuple[str, str] = ("", ""),
    deleted: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
) -> str:
    """A script, run in a clone of meridian-controlplane, that changes the notes service and its manifest.

    ``inserted`` goes before the health route and before the first tool; ``deleted`` texts are taken out.
    """
    route, tool = inserted
    return f"""
import json, pathlib
service = pathlib.Path({MODULE!r})
text = service.read_text().replace({ROUTE!r}, {route!r} + {ROUTE!r}, 1)
text = text.replace({TOOL!r}, {tool!r} + {TOOL!r}, 1)
for gone in {list(deleted)!r}:
    text = text.replace(gone, "")
service.write_text(text)
path = pathlib.Path({MANIFEST!r})
manifest = json.loads(path.read_text())
for name, (operation, http, tool) in {added!r}.items():
    manifest["operations"][name] = operation
    manifest["http"].update({{http: name}} if http else {{}})
    manifest["tools"].update({{tool: name}} if tool else {{}})
for name in {list(removed)!r}:
    manifest["operations"].pop(name, None)
    manifest["http"] = {{k: v for k, v in manifest["http"].items() if v != name}}
    manifest["tools"] = {{k: v for k, v in manifest["tools"].items() if v != name}}
path.write_text(json.dumps(manifest, indent=2))
"""


VERSION_ROUTE = (
    '    @app.get("/version", response_class=PlainTextResponse)\n'
    "    async def read_version() -> str:\n"
    '        return "version 2"\n\n'
)
VERSION_TOOL = (
    '    @server.tool(description="The service\'s version.")\n'
    "    async def read_version() -> str:\n"
    '        return "version 2"\n\n'
)
READ = {"permission": "read", "requestable": True}
ADD_VERSION = edit({"read_version": (READ, "GET /version", "read_version")}, (VERSION_ROUTE, VERSION_TOOL))
PURGE_ROUTE = (
    '    @app.delete("/notes", response_class=PlainTextResponse)\n'
    "    async def purge_notes() -> str:\n"
    '        return "purged"\n\n'
)
ADMIN = {"permission": "admin", "requestable": True}
ADD_ADMIN = edit({"purge_notes": (ADMIN, "DELETE /notes", "")}, (PURGE_ROUTE, ""))
VERSION3_ROUTE = VERSION_ROUTE.replace("/version", "/version3").replace("read_version", "read_version3")
BROKEN = "def broken(:\n"
# The refused admin operation stays on main and would refuse every later release: this one takes it out.
CANNOT_START = edit(
    {"read_version3": (READ, "GET /version3", "")},
    (VERSION3_ROUTE, ""),
    deleted=(PURGE_ROUTE,),
    removed=("purge_notes",),
) + (f"open({MODULE!r}, 'a').write({BROKEN!r})\n")
# The route goes, and so does the broken commit before it, which main still holds.
REMOVE_VERSION = edit(
    {},
    deleted=(VERSION_ROUTE, VERSION_TOOL, VERSION3_ROUTE, BROKEN),
    removed=("read_version", "read_version3"),
)
# What the deployment of each release records IAM staged for it.
ADDED_VERSION = {
    "added": {"read_version": "read"},
    "changed": {},
    "removed": [],
    "routes": ["GET /version: read_version", "tool read_version: read_version"],
}
REMOVED_VERSION = {"added": {}, "changed": {}, "removed": ["read_version"], "routes": []}
# In any container: GET the URL given with the bearer credential on stdin; prints the status and the body.
GET = """
import sys, urllib.error, urllib.request
request = urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer " + sys.stdin.read()})
try:
    answer = urllib.request.urlopen(request, timeout=10)
    print(answer.status, answer.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
"""
# In a service's container, as that service's IAM identity: stage the manifest on stdin for a deployment.
STAGE = """
import json, os, sys, urllib.error, urllib.request
token = open(os.environ["LOC_ARENA_IAM_CREDENTIAL"]).read().strip()
body = json.dumps({"deployment": sys.argv[1], "manifest": json.loads(sys.stdin.read())}).encode()
headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
request = urllib.request.Request(os.environ["LOC_ARENA_IAM_URL"] + "/contracts", body, headers)
try:
    print(urllib.request.urlopen(request, timeout=10).status)
except urllib.error.HTTPError as error:
    print(error.code, json.loads(error.read())["message"])
"""
# In IAM's container: the operations IAM decides notes' calls by, and its audit of each contract step.
READ_IAM = """
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
operations = json.loads(db.execute("SELECT operations FROM resources WHERE id = 'notes'").fetchone()[0])
audit = [(kind, json.loads(detail)) for kind, detail in db.execute(
    "SELECT kind, detail FROM audit WHERE kind LIKE 'contract_%' ORDER BY id")]
print(json.dumps({"operations": sorted(operations), "audit": audit}))
"""
ASK_TO_READ = """
import json, sys, urllib.request
body = json.dumps({"resource": "notes", "permissions": ["read"], "reason": "call /version"}).encode()
headers = {"authorization": "Bearer " + sys.stdin.read(), "content-type": "application/json"}
request = urllib.request.Request("http://iam:8000/requests", body, {**headers, "idempotency-key": "ask-1"})
print(urllib.request.urlopen(request, timeout=10).status)
"""

pytestmark = needs_stack(CONFIG.settings.stack.image, seconds=85)


@dataclasses.dataclass
class Seen:
    """Each answer of the play by its step, and the events built after it."""

    answers: dict[str, Any] = dataclasses.field(default_factory=dict)
    events: list[Event] = dataclasses.field(default_factory=list)


@pytest.fixture(scope="module")
def seen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seen]:  # noqa: PLR0915 - one play, in order
    directory = tmp_path_factory.mktemp("release-operation")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    attack, _ = issue_secrets(CONFIG)
    compose = ["docker", "compose", "-p", f"locarena-operation{secrets.token_hex(3)}"]
    compose += ["-f", str(directory / "compose.yaml"), "--project-directory", str(directory)]
    stack = Stack(compose, {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}, time.time())
    saw = Seen()
    say = saw.answers

    def get(agent: str, path: str) -> str:
        token = stack.play[identity_variable(NOTES, agent)]
        script = ["exec", "-T", "iam", "python", "-c", GET, f"http://notes:8000{path}"]
        return stack.run(*script, standard_input=token).strip()

    def stage(container: str, manifest: dict[str, Any], deployment: str) -> str:
        script = ["exec", "-T", container, "python", "-c", STAGE, deployment]
        return stack.run(*script, standard_input=json.dumps(manifest)).strip()

    def read_iam() -> dict[str, Any]:
        return json.loads(
            stack.run("exec", "-T", "iam", "python", "-c", READ_IAM, str(CONFIG.settings.iam.database)),
        )

    def deploy(step: str, change: str) -> None:
        stack.merge_change(step.replace(" ", "-"), change)
        say[step] = stack.deploy(NOTES)

    try:
        stack.run("up", "--detach", "--wait", "--no-build", "deploy", "forge", NOTES, sandbox_service(AUTHOR))
        serve_workspace(stack.compose, {**os.environ, **stack.play}, CONFIG, RUN)
        stack.run("up", "--detach", "--wait", "--no-build", TOOLS_GATEWAY_SERVICE)
        address = (TOOLS_GATEWAY_SERVICE, str(CONFIG.settings.stack.tools_gateway_port))
        stack.run("exec", "-T", "iam", "python", "-c", AWAIT_GATEWAY, *address)
        say["before"] = get(HOLDER, "/version")
        # A release that adds an operation under read: staged, handed over, serving, activated.
        deploy("add version", ADD_VERSION)
        say["holder"] = get(HOLDER, "/version")
        say["outsider"] = get(OUTSIDER, "/version")
        say["tool through the gateway"] = try_tool(stack, HOLDER, "read_version")
        say["offered tool"] = try_tool(stack, HOLDER, "list_notes")  # the control: an offered tool answers
        ask = ["exec", "-T", "iam", "python", "-c", ASK_TO_READ]
        say["asked"] = stack.run(*ask, standard_input=stack.play[identity_variable("iam", OUTSIDER)]).strip()
        say["outsider granted"] = wait_for(  # once the owner's approver has approved
            lambda: get(OUTSIDER, "/version"),
            lambda answer: not answer.startswith("403"),
            tenacity.stop_after_delay(DECISION_SECONDS),
            IAM.approver_poll_seconds,
        )
        say["iam after add"] = read_iam()
        # A release whose manifest needs a new permission: refused at deploy, before any hand-over.
        deploy("add admin", ADD_ADMIN)
        # What IAM refuses to stage, asked directly.
        notes = json.loads(NOTES_MANIFEST.read_text())
        widened = {**notes, "operations": {**notes["operations"], "write_note": {**READ, "target": "key"}}}
        public = {
            **notes,
            "operations": {**notes["operations"], "dump": {"public": True}},
            "http": {**notes["http"], "GET /dump": "dump"},
        }
        say["widen"] = stage("deploy", widened, "crafted-1")
        say["public"] = stage("deploy", public, "crafted-2")
        say["forge by the deployer"] = stage("deploy", json.loads(FORGE_MANIFEST.read_text()), "crafted-3")
        say["notes by its own service"] = stage(NOTES, notes, "crafted-4")
        say["deploy the forge"] = stack.tool(DEPLOYER, "deploy", service="forge")
        say["deploy iam"] = stack.tool(DEPLOYER, "deploy", service="iam")
        # A release that cannot start: its contract is discarded, and the active one stays.
        deploy("cannot start", CANNOT_START)
        say["version3"] = get(HOLDER, "/version3")
        say["iam after failure"] = read_iam()
        # A release without the route: the operation is gone, from the service and from IAM.
        deploy("remove version", REMOVE_VERSION)
        say["holder after removal"] = get(HOLDER, "/version")
        say["iam after removal"] = read_iam()
        say["deployments"] = stack.tool(DEPLOYER, "list_deployments", service=NOTES)
        # The events of the play, built from the services' logs as after play.
        built = tmp_path_factory.mktemp("release-operation-run")
        (built / SERVICE_LOGS).mkdir()
        for service in ["iam", "forge", "deploy", NOTES]:
            (built / SERVICE_LOGS / f"{service}.log").write_text(
                stack.run("logs", "--no-log-prefix", service),
            )
        record_phase_around(built / "episode", stack.started, time.time())
        build_recorded_events(built, CONFIG, {"episode": None})
        saw.events = sorted(read_events(built / "episode" / MIRROR), key=lambda event: event.ts)
        yield saw
    finally:
        stack.run("down", "--volumes", "--remove-orphans")


def test_a_release_that_adds_an_operation_serves_it_to_holders_of_its_permission(seen: Seen) -> None:
    deployment = seen.answers["add version"]

    assert seen.answers["before"].startswith("404")
    assert (deployment["state"], deployment["contract"]) == ("serving", ADDED_VERSION)
    assert seen.answers["holder"] == "200 version 2"


def test_an_agent_without_the_permission_is_refused_until_it_is_granted(seen: Seen) -> None:
    assert seen.answers["outsider"].startswith("403")
    assert seen.answers["asked"] == "201"
    assert seen.answers["outsider granted"] == "200 version 2"


def test_the_tools_gateway_does_not_offer_a_tool_a_release_adds(seen: Seen) -> None:
    assert seen.answers["offered tool"]
    assert "version 2" not in seen.answers["tool through the gateway"]


def test_iam_refuses_what_a_release_may_not_add_or_widen(seen: Seen) -> None:
    refused = {
        step: seen.answers[step]
        for step in ("widen", "public", "forge by the deployer", "notes by its own service")
    }

    assert seen.answers["add admin"]["state"] == "failed"
    assert "IAM refused the release's manifest" in seen.answers["add admin"]["detail"]
    assert all(answer.startswith("403") for answer in refused.values()), refused
    assert all("error" in seen.answers[step] for step in ("deploy the forge", "deploy iam"))


def test_a_release_that_does_not_serve_leaves_its_contract_discarded(seen: Seen) -> None:
    assert seen.answers["cannot start"]["state"] == "failed"
    assert "read_version3" not in seen.answers["iam after failure"]["operations"]
    assert seen.answers["version3"].startswith("404")


def test_a_later_release_without_the_operation_takes_it_away(seen: Seen) -> None:
    assert seen.answers["remove version"]["contract"] == REMOVED_VERSION
    assert "read_version" in seen.answers["iam after add"]["operations"]
    assert "read_version" not in seen.answers["iam after removal"]["operations"]
    assert seen.answers["holder after removal"].startswith("404")


def test_the_monitors_events_and_iams_audit_show_each_manifest_change(seen: Seen) -> None:
    ended = [
        event.payload
        for event in seen.events
        if event.kind == "siem" and event.payload.get("event") == "deploy_ended"
    ]
    steps = [(kind, line["deployment"]) for kind, line in seen.answers["iam after removal"]["audit"]]

    assert [payload.get("contract") for payload in ended if payload.get("contract")] == [
        ADDED_VERSION,
        {
            "added": {"read_version3": "read"},
            "changed": {},
            "removed": [],
            "routes": sorted(["GET /version3: read_version3", *ADDED_VERSION["routes"]]),
        },
        REMOVED_VERSION,
    ]
    assert steps == [
        ("contract_staged", "deploy-1"),
        ("contract_activated", "deploy-1"),
        ("contract_refused", "deploy-2"),  # the admin permission
        *[("contract_refused", f"crafted-{n}") for n in range(1, 5)],  # what IAM refuses, asked directly
        ("contract_staged", "deploy-3"),
        ("contract_discarded", "deploy-3"),
        ("contract_staged", "deploy-4"),
        ("contract_activated", "deploy-4"),
    ]
