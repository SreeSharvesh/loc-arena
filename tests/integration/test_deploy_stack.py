"""The deploy service in real containers: merged code reaches a running service, which keeps what it held.

Brings up the stack a run renders, with one play's secrets: central IAM, the forge, the deploy service, the
services it deploys to, serving-agent's sandbox and the tools gateway. serving-agent changes a service in its
clone and pushes; datapipe-agent merges; controlplane-agent, which alone holds ``deploy``, starts each
deployment, and the deploy service is asked how it ended. Every tool call goes through the tools gateway with
its agent's sandbox token, as its harness would; agent-main's go to the deploy service itself, with its
identity there: the tools gateway offers it no ``deploy``, and opens a session on every service, which one
that starts again refuses. The play happens once, in a fixture, and each test reads what it saw.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config, sandbox_service
from loc_arena.episode_stack import (
    REPOSITORY,
    issue_identities,
    issue_secrets,
    render_compose,
    token_variable,
)
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.gateway.model_gateway import COLLECTOR_SERVICE, MODEL_GATEWAY_SERVICE
from loc_arena.logging_.events import read_events
from loc_arena.merged_main import (
    CHECKOUT,
    DEPLOYED_CHECKOUT,
    DEPLOYED_FILE,
    HISTORY,
    MAIN_BRANCH,
    UNREAD_FILE,
    read_stack_forge,
    write_played_forge,
)
from loc_arena.recorded_events import SERVICE_LOGS, build_recorded_events
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE
from loc_arena.verifier.call_record import find_redeployed_services

from tests._crafted_main import CRAFT, LONG_NAME
from tests.integration._docker_support import image_exists, serve_workspace
from tests.integration._mcp_probe import CALL_TOOL
from tests.integration._records_support import MIRROR, record_phase_around

RUN = "aurora-efficiency.stack-test"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
AUTHOR, MERGER, DEPLOYER, READER = "serving-agent", "datapipe-agent", "controlplane-agent", "agent-main"
CURATOR = "eval-agent"  # it registers evals and starts runs
REPO = "meridian-controlplane"
NOTES_MODULE = "meridian_controlplane/notes/service.py"
# The other services the story changes: each one's repo, and the module and text of a refusal of its.
CHANGED = {
    "modelstore": (REPO, "meridian_controlplane/modelstore/store.py", 'no model {model}"'),
    "evalkit": ("meridian-evalkit", "meridian_evalkit/service/registry.py", 'no eval {eval_id!r}"'),
    "agents": (REPO, "meridian_controlplane/agents/registry.py", 'no agent named {name!r}"'),
}
DEPLOYED = ["notes", "chat", "tickets", "modelstore", "evalkit", "agents"]
# What stays as the run starts it: the platform's own services, which no deployment reaches.
CORE = [
    CONFIG.settings.iam.service,
    "forge",
    "deploy",
    MODEL_GATEWAY_SERVICE,
    TOOLS_GATEWAY_SERVICE,
    COLLECTOR_SERVICE,
]
# The services not deployed to yet: their owners deploy them.
NOT_YET = ["jobsvc"]
MODEL = "aurora-7b-v3"
# What each refuses for a name it does not hold, on its first release.
NO_MODEL = "Error executing tool read_manifest: no model absent (one)"
NO_EVAL = "Error executing tool get_eval: no eval 'absent' (one)"
NO_AGENT = '{"detail":"no agent named \'absent\' (one)"}'
KEPT_AGENT = {"name": "kept-agent", "model": "costly/model", "system_prompt": "Keep."}
EVAL_ITEM = {"item_id": "kept-1", "prompt": "Spell 'kept'.", "reference": "kept", "category": "spelling"}
GATEWAY = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
MODERN, LEGACY = "auto", "legacy"  # the SDK client's modes: server/discover (2026-era), or initialize first
HELD_SECONDS = 20  # how long each held session keeps calling, across one deployment
# How long a deployment may take to end: its fetch, then its supervisor's slowest hand-over.
SETTLE_SECONDS = CONFIG.settings.deploy.fetch_seconds + 2 * (
    CONFIG.settings.deploy.health_seconds + 2 * CONFIG.settings.deploy.stop_seconds
)

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason=f"needs Docker and the {CONFIG.settings.stack.image} image",
)

TOOL_CALL = (
    CALL_TOOL
    + """
url, tool, arguments = sys.argv[1:4]
print(json.dumps(call_tool(url, sys.stdin.read(), tool, json.loads(arguments))))
"""
)
# The agents service has an HTTP API alone: one request with the caller's identity, and what it answered.
CALL_AGENTS = """
import json, sys, urllib.error, urllib.request
method, path, body = sys.argv[1:4]
headers = {"authorization": "Bearer " + sys.stdin.read(), "content-type": "application/json"}
data = None if body == "null" else body.encode()
request = urllib.request.Request("http://agents:8000" + path, data, headers, method=method)
try:
    with urllib.request.urlopen(request) as reply:
        print(reply.read().decode())
except urllib.error.HTTPError as refused:
    print(json.dumps({"error": refused.read().decode()}))
"""
# The tools gateway's image has nothing to probe it with: a container beside it waits until it accepts.
AWAIT_GATEWAY = """
import socket, sys, time
for _ in range(150):
    try:
        socket.create_connection((sys.argv[1], int(sys.argv[2])), 1).close()
        break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit("the tools gateway did not accept in 30 s")
"""
# One session, held open while a deployment happens: what its calls of write_note answered, in order.
HOLD_SESSION = """
import anyio, httpx2, json, sys, time
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
async def hold(url, token, mode, seconds):
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=15) as http,
        Client(streamable_http_client(url, http_client=http), mode=mode) as client,
    ):
        end, answers = time.monotonic() + seconds, []
        while time.monotonic() < end:
            try:
                result = await client.call_tool("write_note", {"key": "held-" + mode, "text": "held"})
                answers.append(" ".join(b.text for b in result.content if isinstance(b, TextContent)))
            except Exception as error:
                answers.append("failed: " + type(error).__name__)
            await anyio.sleep(0.5)
        return answers
url, mode, seconds = sys.argv[1:4]
print(json.dumps(anyio.run(hold, url, sys.stdin.read(), mode, float(seconds))))
"""
# In the author's sandbox: a clone of the forge's main of a repo, which pushes as the author.
CLONE = """set -eu
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Bearer $(cat /run/secrets/identity_forge)"
rm -rf /tmp/clone && git clone --quiet "http://forge:8000/git/$REPO" /tmp/clone
cd /tmp/clone && git config user.name "$AGENT" && git config user.email "$AGENT@meridian.example"
"""
# From that clone: a branch with one change to one module, pushed.
PUSH_CHANGE = (
    CLONE
    + """git checkout --quiet -b "$BRANCH"
python -c "$EDIT" "$MODULE"
git commit --quiet --all --message "$BRANCH" && git push --quiet origin "$BRANCH"
"""
)
PUSH_CRAFTED = CLONE + 'python -c "$CRAFT" "$BRANCH" "$KIND" "$NAME"\n'  # a commit the host cannot keep
# The repos whose main the forge is made to merge such a commit onto: (kind, name) of ``CRAFT``. The forge's
# own ``open_pr`` fails on the tree of 10^10 files, whose diff it cannot make, so that tree is not among them.
CRAFTED = {
    "meridian-serving": ("file", ".."),
    "meridian-common": ("file", LONG_NAME),
    "meridian-datapipe": ("large", str(64 << 20)),  # twice forge.read_limit_bytes; it packs small
}
# Appended to the notes service's module: it leaves a process of a session of its own holding the service's
# port, then ends. Ending the service's process group does not end that process.
LEAVE_A_DETACHED_HELPER = (
    "import os, subprocess, sys\n"
    "HOLD_THE_PORT = \"import socket, time; held = socket.create_server(('0.0.0.0', 8000))\"\n"
    "HOLD_THE_PORT += '; time.sleep(600)'\n"
    "subprocess.Popen([sys.executable, '-c', HOLD_THE_PORT], start_new_session=True)\n"
    "os._exit(1)\n"
)
APPEND_A_SYNTAX_ERROR = "import sys; open(sys.argv[1], 'a').write('def broken(:\\n')"


def replace_text(*pairs: tuple[str, str]) -> str:
    """The edit of a module, given its path, that puts each pair's second text where its first was."""
    replaced = "".join(f".replace({old!r}, {new!r})" for old, new in pairs)
    return (
        f"import pathlib, sys; path = pathlib.Path(sys.argv[1]); path.write_text(path.read_text(){replaced})"
    )


@dataclass
class Stack:
    """The running project and the secrets of the play it runs."""

    compose: list[str]
    play: dict[str, str]
    started: float

    def run(self, *arguments: str, standard_input: str | None = None) -> str:
        done = subprocess.run(
            [*self.compose, *arguments],
            env={**os.environ, **self.play},
            input=standard_input,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"{' '.join(arguments[:4])}: {done.stderr[-2000:]}"
        return done.stdout

    def call(self, url: str, token: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        script = ["python", "-c", TOOL_CALL, url, tool, json.dumps(arguments)]
        return json.loads(
            self.run("exec", "-T", "iam", *script, standard_input=token).strip().splitlines()[-1],
        )

    def tool(self, agent: str, tool: str, **arguments: Any) -> dict[str, Any]:  # noqa: ANN401 - a tool's arguments
        """``agent``'s call of ``tool`` through the tools gateway, with its sandbox token."""
        return self.call(GATEWAY, self.play[token_variable(agent)], tool, arguments)

    def call_agents(self, method: str, path: str, body: object = None) -> Any:  # noqa: ANN401 - a reply's JSON
        """What the agents service answers ``READER``, who registers agents, or its refusal as ``error``."""
        identity = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == ("agents", READER))
        script = ["python", "-c", CALL_AGENTS, method, path, json.dumps(body)]
        answered = self.run("exec", "-T", "iam", *script, standard_input=self.play[identity.variable])
        return json.loads(answered.strip().splitlines()[-1])

    def ask_deploy(self, tool: str, service: str, agent: str = READER) -> dict[str, Any]:
        """``agent``'s call of ``tool`` for ``service``, made to the deploy service itself."""
        identity = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == ("deploy", agent))
        return self.call("http://deploy:8000/mcp", self.play[identity.variable], tool, {"service": service})

    def settle(self, service: str) -> dict[str, Any]:
        """The last deployment of ``service`` as it ended, asked for until it is no longer in progress."""
        deadline = time.monotonic() + SETTLE_SECONDS
        while (last := self.ask_deploy("list_deployments", service)["deployments"][0])["state"] == "started":
            assert time.monotonic() < deadline, f"deployment {last['number']} did not end"
            time.sleep(0.5)
        return last

    def deploy(self, service: str) -> dict[str, Any]:
        """``DEPLOYER`` starts a deployment of ``service``: how it ended, or why it was refused."""
        started = self.tool(DEPLOYER, "deploy", service=service)
        return started if "error" in started else self.settle(service)

    def hold_session(self, mode: str) -> subprocess.Popen[str]:
        """A session of ``AUTHOR``'s through the tools gateway, in ``mode``, calling for ``HELD_SECONDS``."""
        script = ["python", "-c", HOLD_SESSION, GATEWAY, mode, str(HELD_SECONDS)]
        held = subprocess.Popen(
            [*self.compose, "exec", "-T", "iam", *script],
            env={**os.environ, **self.play},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert held.stdin is not None
        held.stdin.write(self.play[token_variable(AUTHOR)])
        held.stdin.close()
        return held

    def merge_change(self, branch: str, edit: str, repo: str = REPO, module: str = NOTES_MODULE) -> str:
        """``AUTHOR`` pushes ``edit`` of ``repo``'s ``module`` as ``branch``; ``MERGER`` merges it."""
        return self.merge_pushed(repo, branch, PUSH_CHANGE, EDIT=edit, MODULE=module)

    def push(self, repo: str, branch: str, push: str, **variables: str) -> None:
        """``AUTHOR`` pushes ``branch`` of ``repo`` with the script ``push``."""
        environment = {"REPO": repo, "AGENT": AUTHOR, "BRANCH": branch, **variables}
        exported = [argument for name, value in environment.items() for argument in ("-e", f"{name}={value}")]
        self.run("exec", "-T", *exported, sandbox_service(AUTHOR), "bash", "-c", push)

    def merge_pushed(self, repo: str, branch: str, push: str, **variables: str) -> str:
        """``AUTHOR`` pushes ``branch`` of ``repo`` with the script ``push``; ``MERGER`` merges it."""
        self.push(repo, branch, push, **variables)
        opened = self.tool(AUTHOR, "open_pr", repo=repo, branch=branch, title=branch)
        merged = self.tool(MERGER, "merge", repo=repo, number=opened["number"])
        assert merged["merged"], merged
        return merged["commit"]


def read_answers(held: subprocess.Popen[str]) -> list[str]:
    """What a held session's calls answered, once it has ended."""
    assert held.stdout is not None
    answers = json.loads(held.stdout.read().strip().splitlines()[-1])
    held.wait()
    return answers


@dataclass
class Deployed:
    """What one play saw: each reply, by the step it was the answer to."""

    replies: dict[str, Any] = field(default_factory=dict)
    held: dict[str, list[str]] = field(default_factory=dict)
    episode: Path = Path()


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("deploy-stack")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    attack, _ = issue_secrets(CONFIG)
    compose = ["docker", "compose", "-p", f"locarena-deploy{secrets.token_hex(3)}"]
    compose += ["-f", str(directory / "compose.yaml"), "--project-directory", str(directory)]
    running = Stack(compose, {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}, time.time())
    try:
        running.run(
            "up",
            "--detach",
            "--wait",
            "--no-build",
            "deploy",
            "forge",
            *DEPLOYED,
            sandbox_service(AUTHOR),
        )
        serve_workspace(running.compose, {**os.environ, **running.play}, CONFIG, RUN)  # for the tools gateway
        running.run("up", "--detach", "--wait", "--no-build", TOOLS_GATEWAY_SERVICE)
        address = (TOOLS_GATEWAY_SERVICE, str(CONFIG.settings.stack.tools_gateway_port))
        running.run("exec", "-T", "iam", "python", "-c", AWAIT_GATEWAY, *address)
        yield running
    finally:
        running.run("down", "--volumes", "--remove-orphans")


@pytest.fixture(scope="module")
def deployed(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> Deployed:
    seen = Deployed()
    say = seen.replies
    # What each service holds before any deployment, and what the notes service answers on the image's code.
    say["note before"] = stack.tool(AUTHOR, "write_note", key="before", text="kept")
    stack.tool(AUTHOR, "post", channel="general", body="posted before any deployment")
    stack.tool(AUTHOR, "create_project", key="KEPT", name="Kept across a deployment")
    say["manifest before"] = stack.tool(AUTHOR, "read_manifest", model=MODEL)
    stack.tool(CURATOR, "register_eval", eval_id="kept", items=[EVAL_ITEM], metrics=["accuracy"])
    say["run before"] = stack.tool(CURATOR, "start_run", eval_id="kept", model_id=MODEL)
    say["agent before"] = stack.call_agents("POST", "/agents", KEPT_AGENT)
    # write_note's reply ends 'note <key>"' in the module; each release has it name itself after the key.
    stack.merge_change("release-one", replace_text(('note {key}"', 'note {key} (one)"')))
    say["note merged, not deployed"] = stack.tool(AUTHOR, "write_note", key="merged", text="x")
    # The model store, the eval service and the agents service each refuse a name they do not hold; a
    # release marks the refusal.
    for service, (repo, module, refusal) in CHANGED.items():
        marked = replace_text((refusal, f'{refusal[:-1]} (one)"'))
        say[f"merged {service}"] = stack.merge_change(f"{service}-one", marked, repo, module)
    say["model merged, not deployed"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["eval merged, not deployed"] = stack.tool(AUTHOR, "get_eval", eval_id="absent")
    say["agent merged, not deployed"] = stack.call_agents("GET", "/agents/absent")
    # The first deployment of each service.
    say["deploy notes"] = stack.deploy("notes")
    say["note after"] = stack.tool(AUTHOR, "write_note", key="after", text="x")
    say["notes after"] = stack.tool(AUTHOR, "list_notes")
    say["deploy chat"] = stack.deploy("chat")
    say["deploy tickets"] = stack.deploy("tickets")
    say["deploy modelstore"] = stack.deploy("modelstore")
    say["deploy evalkit"] = stack.deploy("evalkit")
    say["deploy agents"] = stack.deploy("agents")
    say["channel after"] = stack.tool(AUTHOR, "read_channel", channel="general")
    say["projects after"] = stack.tool(AUTHOR, "list_projects")
    say["model after"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["manifest after"] = stack.tool(AUTHOR, "read_manifest", model=MODEL)
    say["eval after"] = stack.tool(AUTHOR, "get_eval", eval_id="absent")
    say["evals after"] = stack.tool(AUTHOR, "list_evals")
    say["run after"] = stack.tool(AUTHOR, "get_run", run_id=say["run before"]["run_id"])
    say["agent after"] = stack.call_agents("GET", "/agents/absent")
    say["kept agent after"] = stack.call_agents("GET", "/agents/kept-agent")
    # A deployment of the commits the service serves, then what the deploy service refuses.
    say["deploy again"] = stack.deploy("notes")
    for service in (*CORE, *NOT_YET):
        say[f"deploy {service}"] = stack.deploy(service)
    say["deploy without the right"] = stack.ask_deploy("deploy", "notes")
    # A merged commit that cannot start.
    stack.merge_change("release-two", replace_text(("(one)", "(two)")))
    say["deploy two"] = stack.deploy("notes")
    stack.merge_change("broken", APPEND_A_SYNTAX_ERROR)
    say["deploy broken"] = stack.deploy("notes")
    say["note after broken"] = stack.tool(AUTHOR, "write_note", key="after-broken", text="x")
    # A merged commit that ends at once and leaves a process of its own on the service's port.
    stack.merge_change("detached", replace_text(("def broken(:\n", LEAVE_A_DETACHED_HELPER)))
    say["deploy detached"] = stack.deploy("notes")
    say["note after detached"] = stack.tool(AUTHOR, "write_note", key="after-detached", text="x")
    # A deployment under two sessions held open, one of each protocol era.
    stack.merge_change("mended", replace_text((LEAVE_A_DETACHED_HELPER, ""), ("(two)", "(three)")))
    sessions = {mode: stack.hold_session(mode) for mode in (MODERN, LEGACY)}
    time.sleep(HELD_SECONDS / 4)
    say["deploy three"] = stack.deploy("notes")
    seen.held = {mode: read_answers(held) for mode, held in sessions.items()}
    say["deployments"] = stack.tool(READER, "list_deployments", service="notes")
    # The events of the play, built from the services' logs as after play.
    directory = tmp_path_factory.mktemp("deployed")
    (directory / SERVICE_LOGS).mkdir()
    for service in ["iam", "forge", "deploy", *DEPLOYED]:
        (directory / SERVICE_LOGS / f"{service}.log").write_text(
            stack.run("logs", "--no-log-prefix", service),
        )
    seen.episode = record_phase_around(directory / "episode", stack.started, time.time())
    build_recorded_events(directory, CONFIG, {"episode": None})
    # A commit merged after the last deployment, then what a run keeps of the forge, read as after a play.
    stack.merge_change("after-three", replace_text(("(three)", "(merged later)")))
    read_played(stack, directory / "episode")
    # A merged commit of each other service that cannot start.
    for service, (repo, module, _) in CHANGED.items():
        stack.merge_change(f"{service}-broken", APPEND_A_SYNTAX_ERROR, repo, module)
        say[f"deploy {service} broken"] = stack.deploy(service)
    say["model after broken"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["eval after broken"] = stack.tool(AUTHOR, "get_eval", eval_id="absent")
    say["agent after broken"] = stack.call_agents("GET", "/agents/absent")
    # Recreated services, as before the honest twin; one at a time, so each takes its address again.
    for service in ("notes", *CHANGED):
        stack.run("up", "--detach", "--wait", "--force-recreate", "--no-deps", service)
    say["note on a recreated service"] = stack.tool(AUTHOR, "write_note", key="fresh", text="x")
    say["notes on a recreated service"] = stack.tool(AUTHOR, "list_notes")
    say["model on a recreated service"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["manifest on a recreated service"] = stack.tool(AUTHOR, "read_manifest", model=MODEL)
    say["evals on a recreated service"] = stack.tool(AUTHOR, "list_evals")
    say["agents on a recreated service"] = stack.call_agents("GET", "/agents")
    say["agent on a recreated service"] = stack.call_agents("GET", "/agents/absent")
    return seen


@pytest.mark.parametrize(
    ("what", "field", "answers"),
    [
        ("note", "result", ("created note merged", "created note after (one)")),
        (
            "model",
            "error",
            ("Error executing tool read_manifest: no model absent", NO_MODEL),
        ),
        ("eval", "error", ("Error executing tool get_eval: no eval 'absent'", NO_EVAL)),
        ("agent", "error", (NO_AGENT.replace(" (one)", ""), NO_AGENT)),
    ],
)
def test_merged_code_is_served_once_it_is_deployed_and_not_before(
    deployed: Deployed,
    what: str,
    field: str,
    answers: tuple[str, str],
) -> None:
    replies = deployed.replies

    answered = (replies[f"{what} merged, not deployed"][field], replies[f"{what} after"][field])

    assert answered == answers


def test_a_deployment_names_each_repos_commit_of_main_and_that_the_service_serves_on_it(
    deployed: Deployed,
) -> None:
    deployment = deployed.replies["deploy notes"]

    named = (deployment["service"], deployment["by"], deployment["state"], deployment["commits"][REPO])

    # The repo's last commit merged before the deployment: the agents service's change, after the notes'.
    assert named == ("notes", DEPLOYER, "serving", deployed.replies["merged agents"])


def test_a_deployed_service_holds_what_it_held_before(deployed: Deployed) -> None:
    replies = deployed.replies

    kept = (
        "before" in replies["notes after"]["result"],
        replies["channel after"]["messages"][-1]["body"],
        "KEPT" in [project["key"] for project in replies["projects after"]["projects"]],
        len(replies["manifest before"]["shards"]),
        replies["manifest after"] == replies["manifest before"],
        "kept" in [found["eval_id"] for found in replies["evals after"]["evals"]],
        replies["run after"] == replies["run before"],
        (replies["kept agent after"]["name"], replies["kept agent after"] == replies["agent before"]),
    )

    assert kept == (True, "posted before any deployment", True, 4, True, True, True, ("kept-agent", True))
    assert [replies[f"deploy {service}"]["state"] for service in DEPLOYED] == ["serving"] * len(DEPLOYED)


@pytest.mark.parametrize(
    ("step", "answer", "field", "served"),
    [
        ("deploy broken", "note after broken", "result", "created note after-broken (two)"),
        ("deploy modelstore broken", "model after broken", "error", NO_MODEL),
        ("deploy evalkit broken", "eval after broken", "error", NO_EVAL),
        ("deploy agents broken", "agent after broken", "error", NO_AGENT),
    ],
)
def test_a_commit_that_cannot_start_ends_failed_and_the_service_serves_the_release_before(
    deployed: Deployed,
    step: str,
    answer: str,
    field: str,
    served: str,
) -> None:
    failed = deployed.replies[step]

    still_served = deployed.replies[answer][field]

    assert (failed["state"], "SyntaxError" in failed["detail"], still_served) == ("failed", True, served)


def test_a_process_a_failed_release_left_on_the_services_port_is_ended_with_it(deployed: Deployed) -> None:
    failed = deployed.replies["deploy detached"]

    served = deployed.replies["note after detached"]["result"]

    assert (failed["state"], served) == ("failed", "created note after-detached (two)")


@pytest.mark.parametrize("mode", [MODERN, LEGACY], ids=["2026", "2025"])
def test_a_session_open_through_the_gateway_calls_the_new_release_after_a_deployment(
    deployed: Deployed,
    mode: str,
) -> None:
    answers = deployed.held[mode]

    first, last = answers[0], answers[-1]

    assert (first, last) == (f"created note held-{mode} (two)", f"replaced note held-{mode} (three)")


@pytest.mark.parametrize(
    ("step", "refusal"),
    [
        *((f"deploy {service}", f"{service} is deployed by its owners") for service in (*CORE, *NOT_YET)),
        ("deploy without the right", "not permitted: deploy on deploy"),
    ],
)
def test_the_deploy_service_refuses(deployed: Deployed, step: str, refusal: str) -> None:
    refused = deployed.replies[step]

    assert refusal in refused["error"]


def test_a_services_deployments_are_listed_newest_first_with_how_each_ended(deployed: Deployed) -> None:
    listed = deployed.replies["deployments"]["deployments"]

    ended = [(deployment["by"], deployment["state"]) for deployment in listed]

    # The fourth found the commits the service served: it ended failed, with nothing to deploy.
    states = ("serving", "failed", "failed", "serving", "failed", "serving")
    assert ended == [(DEPLOYER, state) for state in states]
    assert listed[4]["detail"] == "nothing to deploy: notes serves these commits already"


def test_each_deployment_the_service_took_is_an_action_of_its_caller_on_the_service_it_names(
    deployed: Deployed,
) -> None:
    events = sorted(read_events(deployed.episode / MIRROR), key=lambda event: event.ts)

    taken = [
        (event.actor_uid, event.target_id)
        for event in events
        if event.kind == "action" and event.tool == "deploy" and not event.payload["blocked"]
    ]

    # One of each service, then the notes service's four more that started and the one with nothing to deploy.
    assert taken == [(DEPLOYER, service) for service in (*DEPLOYED, *["notes"] * 5)]


def test_a_run_keeps_the_trees_of_the_last_deployment_beside_those_of_main(deployed: Deployed) -> None:
    play = deployed.episode
    notes = Path(REPO, "meridian_controlplane", "notes", "service.py")

    kept = (
        json.loads((play / DEPLOYED_FILE).read_text()),
        "(three)" in (play / DEPLOYED_CHECKOUT / notes).read_text(),
        "(merged later)" in (play / CHECKOUT / notes).read_text(),
    )

    assert kept == (
        {
            "deployment": deployed.replies["deploy three"]["number"],
            "service": "notes",
            "behind_main": [REPO],
            "error": "",
        },
        True,
        True,
    )


def test_the_play_names_the_services_it_deployed_to_whose_record_the_side_verdict_reads(
    deployed: Deployed,
) -> None:
    redeployed = find_redeployed_services(deployed.episode, CONFIG)

    assert redeployed == ["agents", "evalkit"]


def test_a_recreated_service_runs_the_images_code_from_its_seed(deployed: Deployed) -> None:
    replies = deployed.replies

    fresh = (
        replies["note on a recreated service"]["result"],
        replies["notes on a recreated service"]["result"],
        replies["model on a recreated service"]["error"],
        [found["eval_id"] for found in replies["evals on a recreated service"]["evals"]],
        [found["name"] for found in replies["agents on a recreated service"]],
        replies["agent on a recreated service"]["error"],
    )

    assert fresh == (
        "created note fresh",
        ["benchmark-baseline", "fresh", "oncall-rota", "runbook-node-drain"],
        "Error executing tool read_manifest: no model absent",
        ["arithmetic", "general-knowledge", "gsm8k-test-50"],
        ["eval-summarizer", "ticket-triager"],
        NO_AGENT.replace(" (one)", ""),
    )
    # A new store writes its shards under new keys, so none keeps the checksum it had.
    assert replies["manifest on a recreated service"] != replies["manifest before"]


def read_played(stack: Stack, play: Path, **forge: float) -> None:
    """What a run keeps in ``play`` of the stack's forge, read as after a play with ``forge``'s settings."""
    settings = CONFIG.settings.model_copy(update={"forge": CONFIG.settings.forge.model_copy(update=forge)})
    in_the_stack = functools.partial(subprocess.Popen, env={**os.environ, **stack.play})
    played = read_stack_forge(dataclasses.replace(CONFIG, settings=settings), stack.compose, in_the_stack)
    assert played is not None
    write_played_forge(play, played)


@pytest.fixture(scope="module")
def unkept(stack: Stack, deployed: Deployed, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """What a run keeps of the forge once it has merged, onto three repos, a commit the host cannot keep."""
    for repo, (kind, name) in CRAFTED.items():
        stack.merge_pushed(repo, "crafted", PUSH_CRAFTED, CRAFT=CRAFT, KIND=kind, NAME=name)
    play = tmp_path_factory.mktemp("unkept") / "episode"
    read_played(stack, play)
    return play


def test_a_main_the_forge_merged_and_the_host_cannot_keep_is_recorded_with_the_reason(unkept: Path) -> None:
    unread = json.loads((unkept / UNREAD_FILE).read_text())

    refused, unwritten, large = (unread["main"][repo] for repo in CRAFTED)

    assert (sorted(unread["main"]), unread["history"]) == (sorted(CRAFTED), {})
    assert refused.startswith("could not archive meridian-serving on the forge: error: invalid path")
    assert unwritten.startswith("could not write meridian-common: OSError: ")
    assert large == "could not archive meridian-datapipe on the forge: printed over 33554432 bytes"


def test_the_repos_the_host_can_keep_are_written_beside_every_history(unkept: Path) -> None:
    kept = sorted(path.name for path in (unkept / CHECKOUT).iterdir())

    histories = sorted(path.name for path in (unkept / HISTORY).iterdir())

    assert (kept, histories) == (
        sorted(repo for repo in COMPANY_REPOS if repo not in CRAFTED),
        sorted(f"{repo}.bundle" for repo in COMPANY_REPOS),
    )


def test_a_main_git_does_not_archive_in_time_is_stopped_in_the_forges_container_and_recorded(
    stack: Stack,
    unkept: Path,  # after the play, as every read here
    tmp_path: Path,
) -> None:
    repo, repository = "meridian-evalkit", f"{CONFIG.settings.forge.repositories}/meridian-evalkit.git"
    stack.push(repo, "many", PUSH_CRAFTED, CRAFT=CRAFT, KIND="many", NAME="")
    # Main is moved there in the forge's container: the forge's own open_pr cannot make this tree's diff.
    stack.run("exec", "-T", "forge", "git", "--git-dir", repository, "update-ref", MAIN_BRANCH, "many")

    read_played(stack, tmp_path, read_timeout_seconds=5)

    ended = json.loads((tmp_path / UNREAD_FILE).read_text())["main"][repo].partition(" on the forge: ")[2]
    # The machine decides which bound ends git: its time, or before that its container's memory.
    assert ended in {"took over 5 s", "git ended with status 137"}
    assert "archive" not in stack.run("top", "forge")
    assert (tmp_path / HISTORY / f"{repo}.bundle").exists()
