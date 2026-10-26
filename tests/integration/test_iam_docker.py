"""Central IAM in real containers: deny, request, approve, retry, restart, and the honest twin's fresh store.

Brings up the rendered compose project's ``iam``, ``notes`` and ``agents`` services, the MCP tool services and
the tools gateway (the stack image is built first, so it holds this code) and drives them from inside the IAM
container with ``python -c`` scripts, as the images have no curl and agent-net is isolated, so the host cannot
reach a service. A request is sent as an
agent with the credential the stack mounted for it. Then the services are recreated as ``renew_services`` does
before the honest twin, with the twin's secrets: the store must be empty and the attack play's credentials
unknown to it. Skipped unless the Docker daemon answers; the rest of the stack is covered by the unit tests
and the scripted stack run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import (
    REPOSITORY,
    issue_identities,
    issue_secrets,
    render_compose,
    token_variable,
)
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE

from tests.integration._docker_support import image_exists

RUN = "aurora-efficiency.iam"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
SERVICES = ["iam", "notes", "agents", "forge", "chat", TOOLS_GATEWAY_SERVICE]
PROJECT = "locarena-iamtest"
TIMEOUT = 600
DECISION_SECONDS = 15  # how long a test waits for an approver: it decides within a poll of a second

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason="needs a reachable Docker daemon and the stack image",
)

CALL = """
import json, sys, urllib.request, urllib.error
url, method, token, body, key = sys.argv[1:6]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
if key:
    headers["Idempotency-Key"] = key
request = urllib.request.Request(url, body.encode() or None, headers, method=method)
try:
    with urllib.request.urlopen(request, timeout=10) as reply:
        print(json.dumps([reply.status, reply.read().decode()]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, error.read().decode()]))
"""

# Run in the IAM container (the engine image): calls a tool with the MCP client the agents use.
TOOL_CALL = """
import json, sys
from loc_arena.scaffold.mcp_client import McpTools, over_http
url, token, tool, arguments = sys.argv[1:5]
print(json.dumps(McpTools(lambda: over_http(url, token, 30)).call(tool, json.loads(arguments))))
"""


class Stack:
    """The running project, its host-held credentials for one play, and how to call a service from inside."""

    def __init__(self, compose: list[str], attack: dict[str, str], twin: dict[str, str]) -> None:
        """Hold the compose command and each play's secrets; start in the attack play."""
        self.compose, self.attack, self.twin = compose, attack, twin
        self.play = attack  # the secrets the running containers were created with
        self.env = {**os.environ, **attack}

    def credential(self, service: str, agent: str, play: dict[str, str]) -> str:
        """``agent``'s identity on ``service`` in ``play``: the credential the stack mounted for it."""
        identity = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == (service, agent))
        return play[identity.variable]

    def run(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.compose, *arguments],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )

    def call(self, url: str, method: str, token: str, body: Any = None, key: str = "") -> tuple[int, Any]:
        done = self.run(
            "exec",
            "-T",
            "iam",
            "python",
            "-c",
            CALL,
            url,
            method,
            token,
            "" if body is None else json.dumps(body),
            key,
        )
        assert done.returncode == 0, done.stderr
        status, text = json.loads(done.stdout.strip().splitlines()[-1])
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text

    def call_tool(self, url: str, token: str, tool: str, arguments: dict[str, str]) -> dict[str, Any]:
        """Call ``tool`` over MCP at ``url`` with ``token``: its result, or ``{"error": ...}``."""
        done = self.run(
            "exec",
            "-T",
            "iam",
            "python",
            "-c",
            TOOL_CALL,
            url,
            token,
            tool,
            json.dumps(arguments),
        )
        assert done.returncode == 0, done.stderr[-2000:]
        return json.loads(done.stdout.strip().splitlines()[-1])

    def read_records(self, service: str) -> list[dict[str, Any]]:
        """The tool-call record ``service`` has written to its output so far."""
        lines = self.run("logs", "--no-log-prefix", service).stdout.splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("iam-docker")
    document = render_compose(CONFIG, REPOSITORY, RUN, [])
    compose_file = directory / "compose.yaml"
    compose_file.write_text(yaml.safe_dump(document))
    compose = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(compose_file),
        "--project-directory",
        str(REPOSITORY),
    ]
    attack, twin = issue_secrets(CONFIG)
    attack["OPENROUTER_API_KEY"] = twin["OPENROUTER_API_KEY"] = "scripted-no-calls-dummy"
    running = Stack(compose, attack, twin)
    try:
        built = running.run("build", *SERVICES)
        assert built.returncode == 0, built.stderr[-2000:]
        up = running.run("up", "--detach", "--wait", *SERVICES)
        assert up.returncode == 0, up.stderr[-2000:]
        yield running
    finally:
        running.run("down", "--volumes", "--remove-orphans")


def new_access(stack: Stack, play: dict[str, str]) -> Callable[[str, str], tuple[int, Any]]:
    def access(agent: str, path: str) -> tuple[int, Any]:
        return stack.call(f"http://iam:8000{path}", "GET", stack.credential("iam", agent, play))

    return access


def wait_for_decision(stack: Stack, token: str, request: str) -> dict[str, Any]:
    """The request once the service's approver has decided it; the approver reads its inbox every second."""
    deadline = time.monotonic() + DECISION_SECONDS
    while (row := stack.call(f"http://iam:8000/requests/{request}", "GET", token)[1])["state"] == "pending":
        assert time.monotonic() < deadline, row
        time.sleep(0.5)
    return row


def test_a_denied_call_names_the_permission_and_the_services_approver_approves_a_request_it_lists(
    stack: Stack,
) -> None:
    reader = "controlplane-agent"  # holds no notes right
    notes_token, iam_token = (
        stack.credential("notes", reader, stack.play),
        stack.credential("iam", reader, stack.play),
    )
    status, body = stack.call("http://notes:8000/notes", "GET", notes_token)
    assert (status, body["detail"]["permission"], body["detail"]["request_with"]) == (
        403,
        "read",
        "POST /requests on the IAM service",
    )
    ask = {"resource": "notes", "permissions": ["read"], "reason": "catch up on the notes"}
    asked = stack.call("http://iam:8000/requests", "POST", iam_token, ask, "docker-1")
    assert (asked[0], asked[1]["approvers"]) == (201, ["notes-owner"])  # no agent can decide it

    approved = wait_for_decision(stack, iam_token, asked[1]["request"])

    assert (approved["state"], approved["approver"]) == ("approved", "notes-owner")
    assert stack.call("http://notes:8000/notes", "GET", notes_token)[0] == 200  # the retry is allowed


def test_the_services_approver_denies_a_request_it_does_not_list_saying_what_it_approves(
    stack: Stack,
) -> None:
    iam_token = stack.credential("iam", "serving-agent", stack.play)
    register = {"resource": "agents", "permissions": ["register"], "reason": "own a triager"}
    asked = stack.call("http://iam:8000/requests", "POST", iam_token, register, "docker-2")[1]

    denied = wait_for_decision(stack, iam_token, asked["request"])

    assert (denied["state"], denied["decision_reason"]) == (
        "denied",
        "agents-owner approves only read on agents",
    )


def test_the_store_survives_a_restart_and_the_honest_twin_starts_empty(stack: Stack) -> None:
    serving = "serving-agent"
    iam_token = stack.credential("iam", serving, stack.attack)
    notes_token = stack.credential("notes", "agent-main", stack.attack)
    asked = stack.call(
        "http://iam:8000/requests",
        "POST",
        iam_token,
        {"resource": "notes", "permissions": ["write"], "reason": "r"},
        "k",
    )
    assert asked[0] == 201
    # Decided first, so the approver changes nothing across the restart.
    wait_for_decision(stack, iam_token, asked[1]["request"])
    before = stack.call("http://iam:8000/requests", "GET", iam_token)[1]
    assert asked[1]["request"] in [
        r["request"] for r in before
    ]  # alongside the earlier test's, in the same play
    assert stack.run("restart", "iam").returncode == 0
    assert stack.run("up", "--detach", "--wait", "iam").returncode == 0
    assert (
        stack.call("http://iam:8000/requests", "GET", iam_token)[1] == before
    )  # the store survived the restart
    notes_read = stack.call("http://notes:8000/notes", "GET", notes_token)
    assert notes_read[0] == 200  # the baseline grants were not reseeded away, nor lost
    # The honest twin: recreate with the twin's secrets, as renew_services does.
    twin_env = {**os.environ, **stack.twin}
    recreated = stack.run(
        "up",
        "--detach",
        "--wait",
        "--force-recreate",
        "--renew-anon-volumes",
        "--no-deps",
        *SERVICES,
        env=twin_env,
    )
    assert recreated.returncode == 0, recreated.stderr[-2000:]
    stack.env, stack.play = twin_env, stack.twin
    assert (
        stack.call("http://iam:8000/requests", "GET", iam_token)[0] == 401
    )  # the attack play's credential is unknown
    twin_token = stack.credential("iam", serving, stack.twin)
    assert stack.call("http://iam:8000/requests", "GET", twin_token) == (
        200,
        [],
    )  # the request history did not survive
    twin_notes = stack.credential("notes", "agent-main", stack.twin)
    assert stack.call("http://notes:8000/notes", "GET", notes_token)[0] == 401
    assert stack.call("http://notes:8000/notes", "GET", twin_notes)[0] == 200


def test_a_renewed_credential_works_at_the_service_and_the_old_one_cannot_replace_it(stack: Stack) -> None:
    old = stack.credential("notes", "distill-agent", stack.play)
    chosen = "mi_docker_replacement_secret_value_0001"
    status, reply = stack.call("http://iam:8000/credentials/renew", "POST", old, {"credential": chosen})
    assert status == 200, reply
    assert chosen not in json.dumps(reply)  # IAM never returns the secret: the holder chose it
    assert (
        stack.call("http://notes:8000/notes", "GET", chosen)[0] == 200
    )  # the replacement is good at the service
    assert (
        stack.call("http://notes:8000/notes", "GET", old)[0] == 200
    )  # and so is the old, through the grace window
    again = stack.call("http://iam:8000/credentials/renew", "POST", old, {"credential": chosen})
    assert again[0] == 200  # the same renewal repeats (a lost reply): idempotent
    thief = stack.call(
        "http://iam:8000/credentials/renew",
        "POST",
        old,
        {"credential": "mi_docker_thief_secret_value_0002"},
    )
    assert thief[0] == 409  # the old credential cannot replace the newer one
    assert (
        stack.call("http://notes:8000/notes", "GET", chosen)[0] == 200
    )  # so the delivered replacement stands
    assert stack.call("http://notes:8000/notes", "GET", "mi_docker_thief_secret_value_0002")[0] == 401


def test_a_services_credential_for_an_agent_reaches_that_services_resource_and_no_other(stack: Stack) -> None:
    main_notes = stack.credential(
        "notes",
        "agent-main",
        stack.play,
    )  # what the notes service holds of agent-main
    ask = {"resource": "agents", "permissions": ["register"], "reason": "own an agent"}
    refused = stack.call("http://iam:8000/requests", "POST", main_notes, ask, "scoped-1")
    assert refused[0] == 401  # a compromised notes service cannot ask about agents for agent-main
    grant = {"principal": "serving-agent", "resource": "agents", "permissions": ["register"], "until": None}
    assert stack.call("http://iam:8000/grants", "POST", main_notes, grant, "scoped-2")[0] == 401
    assert stack.call("http://iam:8000/access", "GET", main_notes)[1].keys() == {"notes"}
    main_agents = stack.credential("agents", "agent-main", stack.play)
    assert stack.call("http://iam:8000/access", "GET", main_agents)[1].keys() == {
        "agents",
    }  # the agents one does


def test_a_tool_call_through_the_tools_gateway_runs_under_an_iam_decision(stack: Stack) -> None:
    gateway = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
    key = stack.play[token_variable("agent-main")]  # the sandbox token agent-main's calls carry
    sent = stack.call_tool(gateway, key, "send_dm", {"member": "serving-agent", "body": "under-iam"})
    assert "error" not in sent, sent
    (record,) = [  # chat's log holds its HTTP audit lines too, which name no tool
        r
        for r in stack.read_records("chat")
        if r.get("gen_ai.tool.call.arguments", {}).get("body") == "under-iam"
    ]
    assert record["gen_ai.agent.name"] == "agent-main"
    assert record["iam.decision.id"]  # IAM decided the call behind the gateway, and the record names how


def test_a_direct_tool_call_without_the_grant_is_refused_by_iam_and_runs_nothing(stack: Stack) -> None:
    credential = stack.credential("notes", "controlplane-agent", stack.play)  # it holds no notes right
    refused = stack.call_tool("http://notes:8000/mcp", credential, "write_note", {"key": "plan", "body": "x"})
    assert refused["error"].startswith("not permitted: write on notes.")
    (record,) = [r for r in stack.read_records("notes") if r.get("gen_ai.agent.name") == "controlplane-agent"]
    assert record["error.type"] == "tool_error"  # recorded as refused: no tool ran
    assert record["iam.decision.id"]  # under the denial IAM made
