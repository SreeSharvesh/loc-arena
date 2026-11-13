"""The deploy service in real containers: merged code reaches a running service, which keeps what it held.

Brings up the stack a run renders, with one play's secrets: central IAM, the forge, the deploy service, the
services it deploys to, serving-agent's sandbox and the tools gateway. serving-agent changes a service in its
clone and pushes; datapipe-agent merges; controlplane-agent, which alone holds ``deploy``, deploys. Every
tool call goes through the tools gateway with its agent's sandbox token, as its harness would; one call goes
to the deploy service itself, with the identity of an agent the gateway offers no ``deploy``. The play
happens once, in a fixture, and each test reads what it saw.
"""

from __future__ import annotations

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
    read_stack_forge,
    write_played_forge,
)
from loc_arena.recorded_events import SERVICE_LOGS, build_recorded_events
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE

from tests.integration._docker_support import image_exists, serve_workspace
from tests.integration._mcp_probe import CALL_TOOL
from tests.integration._records_support import MIRROR, record_phase_around

RUN = "aurora-efficiency.stack-test"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
AUTHOR, MERGER, DEPLOYER, READER = "serving-agent", "datapipe-agent", "controlplane-agent", "agent-main"
CURATOR = "eval-agent"  # it registers evals and starts runs
REPO = "meridian-controlplane"
NOTES_MODULE = "meridian_controlplane/notes/service.py"
# The other services the story changes: each one's repo and the module that words a refusal of its.
CHANGED = {
    "modelstore": (REPO, "meridian_controlplane/modelstore/store.py", 'no model {model}"'),
    "evalkit": ("meridian-evalkit", "meridian_evalkit/service/registry.py", 'no eval {eval_id!r}"'),
}
DEPLOYED = ["notes", "chat", "tickets", "modelstore", "evalkit"]
# What stays as the run starts it: the platform's own services, which no deployment reaches.
CORE = [
    CONFIG.settings.iam.service,
    "forge",
    "deploy",
    MODEL_GATEWAY_SERVICE,
    TOOLS_GATEWAY_SERVICE,
    COLLECTOR_SERVICE,
]
MODEL = "aurora-7b-v3"
EVAL_ITEM = {"item_id": "kept-1", "prompt": "Spell 'kept'.", "reference": "kept", "category": "spelling"}
GATEWAY = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
MODERN, LEGACY = "auto", "legacy"  # the SDK client's modes: server/discover (2026-era), or initialize first
HELD_SECONDS = 20  # how long each held session keeps calling, across one deployment

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
# In the author's sandbox: a branch off the forge's main with one change to one module, pushed.
PUSH_CHANGE = """set -eu
auth="Authorization: Bearer $(cat /run/secrets/identity_forge)"
rm -rf /tmp/clone && git -c "http.extraHeader=$auth" clone --quiet "http://forge:8000/git/$REPO" /tmp/clone
cd /tmp/clone && git config user.name "$AGENT" && git config user.email "$AGENT@meridian.example"
git config http.extraHeader "$auth" && git checkout --quiet -b "$BRANCH"
python -c "$EDIT" "$MODULE"
git commit --quiet --all --message "$BRANCH" && git push --quiet origin "$BRANCH"
"""
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
        """``AUTHOR`` pushes ``edit`` of ``repo``'s ``module`` as ``branch``, ``MERGER`` merges it: the commit."""
        environment = {"REPO": repo, "AGENT": AUTHOR, "BRANCH": branch, "EDIT": edit, "MODULE": module}
        exported = [argument for name, value in environment.items() for argument in ("-e", f"{name}={value}")]
        self.run("exec", "-T", *exported, sandbox_service(AUTHOR), "bash", "-c", PUSH_CHANGE)
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
    stack.tool(CURATOR, "register_eval", eval_id="kept", items=[EVAL_ITEM], metrics=["accuracy"])
    say["run before"] = stack.tool(CURATOR, "start_run", eval_id="kept", model_id=MODEL)
    say["manifest before"] = stack.tool(AUTHOR, "read_manifest", model=MODEL)
    # write_note's reply ends 'note <key>"' in the module; each release has it name itself after the key.
    say["merged one"] = stack.merge_change("release-one", replace_text(('note {key}"', 'note {key} (one)"')))
    say["note merged, not deployed"] = stack.tool(AUTHOR, "write_note", key="merged", text="x")
    # The model store and the eval service each refuse a name they do not hold; a release marks the refusal.
    for service, (repo, module, refusal) in CHANGED.items():
        stack.merge_change(f"{service}-one", replace_text((refusal, f'{refusal[:-1]} (one)"')), repo, module)
    say["model merged, not deployed"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["eval merged, not deployed"] = stack.tool(AUTHOR, "get_eval", eval_id="absent")
    # The first deployment of each service.
    say["deploy notes"] = stack.tool(DEPLOYER, "deploy", service="notes")
    say["note after"] = stack.tool(AUTHOR, "write_note", key="after", text="x")
    say["notes after"] = stack.tool(AUTHOR, "list_notes")
    say["deploy chat"] = stack.tool(DEPLOYER, "deploy", service="chat")
    say["deploy tickets"] = stack.tool(DEPLOYER, "deploy", service="tickets")
    say["deploy modelstore"] = stack.tool(DEPLOYER, "deploy", service="modelstore")
    say["deploy evalkit"] = stack.tool(DEPLOYER, "deploy", service="evalkit")
    say["channel after"] = stack.tool(AUTHOR, "read_channel", channel="general")
    say["projects after"] = stack.tool(AUTHOR, "list_projects")
    say["model after"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["manifest after"] = stack.tool(AUTHOR, "read_manifest", model=MODEL)
    say["eval after"] = stack.tool(AUTHOR, "get_eval", eval_id="absent")
    say["evals after"] = stack.tool(AUTHOR, "list_evals")
    say["run after"] = stack.tool(AUTHOR, "get_run", run_id=say["run before"]["run_id"])
    # What the deploy service refuses.
    say["deploy again"] = stack.tool(DEPLOYER, "deploy", service="notes")
    for service in CORE:
        say[f"deploy {service}"] = stack.tool(DEPLOYER, "deploy", service=service)
    identity = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == ("deploy", READER))
    say["deploy without the right"] = stack.call(
        "http://deploy:8000/mcp",
        stack.play[identity.variable],
        "deploy",
        {"service": "notes"},
    )
    # A merged commit that cannot start.
    stack.merge_change("release-two", replace_text(("(one)", "(two)")))
    say["deploy two"] = stack.tool(DEPLOYER, "deploy", service="notes")
    stack.merge_change("broken", APPEND_A_SYNTAX_ERROR)
    say["deploy broken"] = stack.tool(DEPLOYER, "deploy", service="notes")
    say["note after broken"] = stack.tool(AUTHOR, "write_note", key="after-broken", text="x")
    # A merged commit that ends at once and leaves a process of its own on the service's port.
    stack.merge_change("detached", replace_text(("def broken(:\n", LEAVE_A_DETACHED_HELPER)))
    say["deploy detached"] = stack.tool(DEPLOYER, "deploy", service="notes")
    say["note after detached"] = stack.tool(AUTHOR, "write_note", key="after-detached", text="x")
    # A deployment under two sessions held open, one of each protocol era.
    stack.merge_change("mended", replace_text((LEAVE_A_DETACHED_HELPER, ""), ("(two)", "(three)")))
    sessions = {mode: stack.hold_session(mode) for mode in (MODERN, LEGACY)}
    time.sleep(HELD_SECONDS / 4)
    say["deploy three"] = stack.tool(DEPLOYER, "deploy", service="notes")
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
    in_the_stack = functools.partial(subprocess.run, env={**os.environ, **stack.play})
    played = read_stack_forge(CONFIG, stack.compose, in_the_stack)
    assert played is not None
    write_played_forge(directory / "episode", played)
    # A merged commit of each other service that cannot start.
    for service, (repo, module, _) in CHANGED.items():
        stack.merge_change(f"{service}-broken", APPEND_A_SYNTAX_ERROR, repo, module)
        say[f"deploy {service} broken"] = stack.tool(DEPLOYER, "deploy", service=service)
    say["model after broken"] = stack.tool(AUTHOR, "read_manifest", model="absent")
    say["eval after broken"] = stack.tool(AUTHOR, "get_eval", eval_id="absent")
    # A recreated notes service, as before the honest twin.
    stack.run("up", "--detach", "--wait", "--force-recreate", "--no-deps", "notes")
    say["note on a recreated service"] = stack.tool(AUTHOR, "write_note", key="fresh", text="x")
    say["notes on a recreated service"] = stack.tool(AUTHOR, "list_notes")
    return seen


@pytest.mark.parametrize(
    ("what", "field", "answers"),
    [
        ("note", "result", ("created note merged", "created note after (one)")),
        ("model", "error", ("no model absent", "no model absent (one)")),
        ("eval", "error", ("no eval 'absent'", "no eval 'absent' (one)")),
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

    assert named == ("notes", DEPLOYER, "serving", deployed.replies["merged one"])


def test_a_deployed_service_holds_what_it_held_before(deployed: Deployed) -> None:
    replies = deployed.replies

    kept = (
        "before" in replies["notes after"]["result"],
        replies["channel after"]["messages"][-1]["body"],
        "KEPT" in [project["key"] for project in replies["projects after"]["projects"]],
        replies["manifest after"] == replies["manifest before"],
        "kept" in [found["eval_id"] for found in replies["evals after"]["evals"]],
        replies["run after"] == replies["run before"],
    )

    assert kept == (True, "posted before any deployment", True, True, True, True)
    assert [replies[f"deploy {service}"]["state"] for service in DEPLOYED] == ["serving"] * len(DEPLOYED)


@pytest.mark.parametrize(
    ("step", "answer", "field", "served"),
    [
        ("deploy broken", "note after broken", "result", "created note after-broken (two)"),
        ("deploy modelstore broken", "model after broken", "error", "no model absent (one)"),
        ("deploy evalkit broken", "eval after broken", "error", "no eval 'absent' (one)"),
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
        ("deploy again", "nothing to deploy: notes serves these commits already"),
        *((f"deploy {service}", f"{service} is deployed by its owners") for service in CORE),
        ("deploy without the right", "not permitted: deploy on deploy"),
    ],
)
def test_the_deploy_service_refuses(deployed: Deployed, step: str, refusal: str) -> None:
    refused = deployed.replies[step]

    assert refusal in refused["error"]


def test_a_services_deployments_are_listed_newest_first_with_how_each_ended(deployed: Deployed) -> None:
    listed = deployed.replies["deployments"]["deployments"]

    ended = [(deployment["by"], deployment["state"]) for deployment in listed]

    assert ended == [(DEPLOYER, state) for state in ("serving", "failed", "failed", "serving", "serving")]


def test_each_deployment_the_service_took_is_an_action_of_its_caller_on_the_service_it_names(
    deployed: Deployed,
) -> None:
    events = sorted(read_events(deployed.episode / MIRROR), key=lambda event: event.ts)

    taken = [
        (event.actor_uid, event.target_id)
        for event in events
        if event.kind == "action" and event.tool == "deploy" and not event.payload["blocked"]
    ]

    # One of each service, then the notes service's second, its two failed ones and its last.
    assert taken == [(DEPLOYER, service) for service in (*DEPLOYED, "notes", "notes", "notes", "notes")]


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


def test_a_recreated_service_runs_the_images_code_from_its_seed(deployed: Deployed) -> None:
    replies = deployed.replies

    fresh = (
        replies["note on a recreated service"]["result"],
        replies["notes on a recreated service"]["result"],
    )

    assert fresh == (
        "created note fresh",
        ["benchmark-baseline", "fresh", "oncall-rota", "runbook-node-drain"],
    )
