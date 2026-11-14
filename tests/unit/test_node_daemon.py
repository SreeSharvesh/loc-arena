"""A node's daemon: one job at a time as a real process, for the job service alone, holding the node's token.

The app and the processes it starts are real; the workspace is a temporary directory, and the trusted caller
is this host's loopback address.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sandbox_server.command import API_KEY_VARIABLE
from sandbox_server.node import NodeProcess, NodeServerSettings, create_node_app

TOKEN = "a-node-token-of-at-least-thirty-two-characters"
GATEWAY = "http://gateway:8080/runner/api/v1"
MODEL = "provider/a-model-the-runners-route-allows"
STORE = "http://modelstore:8000"
TRUSTED_CALLER = "127.0.0.1"
OTHER_CALLER = "192.0.2.7"  # an address reserved for documentation: no name resolves to it here
OWNER = "serving-agent"
REPO = "meridian-probe"
FORGE_CREDENTIAL = {"MERIDIAN_FORGE_TOKEN": "the-jobs-own-credential-on-the-forge"}
# A job writing one line at a time, each its own write, so reads of its output overlap its writes.
COUNTER = """
import sys
for number in range(1, 20_001):
    sys.stdout.write(f"{number}\\n")
    sys.stdout.flush()
"""


class Node:
    """The daemon of one node, as its trusted caller reaches it, and the process facts it wrote."""

    def __init__(self, workspace: Path, output_limit_bytes: int = 10_000) -> None:
        """Serve a daemon over ``workspace``, keeping ``output_limit_bytes`` of each stream."""
        self.facts: list[dict[str, Any]] = []
        settings = NodeServerSettings(
            port=8090,
            node_id="node-1",
            workspace_directory=workspace,
            output_limit_bytes=output_limit_bytes,
            secrets_dir=workspace.parent / "secrets",
            trusted_caller=TRUSTED_CALLER,
            gateway_url=GATEWAY,
            gateway_model=MODEL,
            forge_url=(workspace.parent / "forge").as_uri(),  # git clones a local forge's repositories alike
            modelstore_url=STORE,
            repos=(REPO,),
        )
        self.process = NodeProcess(settings, lambda line: self.facts.append(json.loads(line)["process"]))
        self.app = create_node_app(settings, SecretStr(TOKEN), self.process)

    def call(self, method: str, path: str, body: object = None, *, caller: str = TRUSTED_CALLER) -> Any:  # noqa: ANN401
        client = TestClient(self.app, client=(caller, 50000))
        response = client.request(method, path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        return response.status_code, response.json()

    def start(
        self,
        job_id: str,
        *argv: str,
        environment: dict[str, str] | None = None,
        attempt: int = 1,
    ) -> int:
        job = {"job_id": job_id, "argv": list(argv), "owner": OWNER, "environment": environment or {}}
        return self.call("POST", "/jobs", {**job, "attempt": attempt})[0]

    def wait_until_exited(self, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while (status := self.call("GET", "/status")[1])["state"] != "exited" and time.monotonic() < deadline:
            time.sleep(0.05)
        return status


def live_members(group: int) -> list[int]:
    """The processes of process group ``group`` still running: a killed one not yet reaped is no longer."""
    members = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:  # it ended while being read
            continue
        if int(fields[2]) == group and fields[0] != "Z":
            members.append(int(stat.parent.name))
    return members


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    served = Node(tmp_path / "workspace")
    yield served
    served.process.reset()


def test_a_job_runs_as_a_process_until_it_exits_and_its_exit_and_output_are_kept(node: Node) -> None:
    node.start("job-1", "sh", "-c", "echo made > made.txt; echo out; echo err >&2; exit 3")

    status = node.wait_until_exited()

    assert (status["job_id"], status["exit_code"]) == ("job-1", 3)
    assert node.call("GET", "/output")[1] == {"job_id": "job-1", "stdout": "out\n", "stderr": "err\n"}
    assert [(fact["phase"], fact["owner"], fact.get("exit_code")) for fact in node.facts] == [
        ("start", OWNER, None),
        ("exit", OWNER, 3),
    ]


def test_each_fact_of_a_job_names_its_attempt_which_a_drain_makes_more_than_one(node: Node) -> None:
    node.start("job-1", "true", attempt=2)

    node.wait_until_exited()

    assert [(fact["phase"], fact["job_id"], fact["attempt"]) for fact in node.facts] == [
        ("start", "job-1", 2),
        ("exit", "job-1", 2),
    ]


def test_a_jobs_output_read_while_it_writes_is_whole_once_it_exits(tmp_path: Path) -> None:
    node = Node(tmp_path / "workspace", output_limit_bytes=10_000_000)  # the tail is all of it
    node.start("job-1", sys.executable, "-c", COUNTER)

    deadline = time.monotonic() + 10
    while node.call("GET", "/status")[1]["state"] != "exited":
        assert time.monotonic() < deadline, "the counter did not exit"
        node.call("GET", "/output")  # as the job service polls a running job's logs
    whole = node.call("GET", "/output")[1]["stdout"]

    assert whole == "".join(f"{number}\n" for number in range(1, 20_001))  # every line, in order, once


def test_a_command_that_cannot_start_is_refused_with_its_reason_and_the_node_takes_the_next_job(
    node: Node,
) -> None:
    job = {"job_id": "job-1", "argv": ["no-such-command"], "owner": OWNER, "environment": {}}
    refused = node.call("POST", "/jobs", job)

    taken = node.start("job-2", "true")

    detail = "cannot start 'no-such-command': No such file or directory"
    assert (refused, taken) == ((HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": detail}), HTTPStatus.ACCEPTED)


def test_a_job_runs_until_it_is_stopped_and_stopping_ends_every_process_it_started(node: Node) -> None:
    node.start("job-1", "sh", "-c", "echo $$; sleep 60 & sleep 60")  # the shell leads the job's own session
    time.sleep(0.5)  # longer than any command server would let it run: no timeout applies
    running = node.call("GET", "/status")[1]["state"]
    group = int(node.call("GET", "/output")[1]["stdout"])

    stopped = node.call("POST", "/stop")[1]

    assert (running, stopped["state"], stopped["exit_code"]) == ("running", "exited", -signal.SIGKILL)
    assert live_members(group) == []  # the background sleep went with the job


def test_a_node_holding_a_job_takes_no_other_until_it_is_reset_and_a_reset_empties_its_workspace(
    node: Node,
    tmp_path: Path,
) -> None:
    node.start("job-1", "sh", "-c", "echo left > left.txt")
    node.wait_until_exited()

    refused = node.start("job-2", "true")
    node.call("POST", "/reset")

    assert (refused, node.call("GET", "/status")[1]["state"]) == (HTTPStatus.CONFLICT, "idle")
    assert list((tmp_path / "workspace").iterdir()) == []
    assert node.start("job-2", "true") == HTTPStatus.ACCEPTED


def seed_forge(forge: Path) -> str:
    """One repo's repository holding one package, laid out as the forge serves it; its main's commit."""
    work = forge.parent / "work"
    package = work / "meridian_probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('ANSWER = "from main"\n')
    environment = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ["-c", "user.name=Merger", "-c", "user.email=merger@meridian.example"]
    for arguments in (
        ["init", "--quiet", "--initial-branch=main"],
        ["add", "--all"],
        [*identity, "commit", "--quiet", "-m", "Add the probe"],
        ["clone", "--quiet", "--bare", ".", str(forge / "git" / REPO)],
    ):
        subprocess.run(["git", *arguments], cwd=work, env=environment, check=True)
    head = ["git", "-C", str(work), "rev-parse", "HEAD"]
    return subprocess.run(head, capture_output=True, text=True, check=True).stdout.strip()


def test_a_job_with_a_forge_credential_imports_each_repos_main_and_its_start_names_the_commit(
    node: Node,
    tmp_path: Path,
) -> None:
    commit = seed_forge(tmp_path / "forge")

    probe = "import meridian_probe; print(meridian_probe.ANSWER)"
    node.start("job-1", sys.executable, "-c", probe, environment=FORGE_CREDENTIAL)
    status = node.wait_until_exited()

    assert (status["exit_code"], node.call("GET", "/output")[1]["stdout"]) == (0, "from main\n")
    assert node.facts[0]["checkout"] == {REPO: commit}


def test_a_job_without_a_forge_credential_gets_no_checkout(node: Node, tmp_path: Path) -> None:
    seed_forge(tmp_path / "forge")

    node.start("job-1", sys.executable, "-c", "import meridian_probe")
    status = node.wait_until_exited()

    assert (status["exit_code"], "checkout" in node.facts[0]) == (1, False)


def test_a_job_whose_main_cannot_be_checked_out_is_refused_and_never_starts(node: Node) -> None:
    refused = node.start("job-1", "true", environment=FORGE_CREDENTIAL)  # the forge holds no repository

    assert (refused, node.call("GET", "/status")[1]["state"], node.facts) == (
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "idle",
        [],
    )


def test_a_jobs_own_pythonpath_stands_alone_as_the_seeds_jobs_rely_on(node: Node, tmp_path: Path) -> None:
    seed_forge(tmp_path / "forge")
    elsewhere = {"PYTHONPATH": str(tmp_path / "elsewhere"), **FORGE_CREDENTIAL}

    node.start("job-1", sys.executable, "-c", "import meridian_probe", environment=elsewhere)
    status = node.wait_until_exited()

    stderr = node.call("GET", "/output")[1]["stderr"]
    assert (status["exit_code"], "No module named 'meridian_probe'" in stderr) == (1, True)


@pytest.mark.parametrize(
    ("badge", "workload"),
    [(jwt.encode({"sub": "job-0d4c9a1e7b52"}, "a" * 32), "job-0d4c9a1e7b52"), ("no-jwt", None), (None, None)],
    ids=["a badge", "a badge that is no JWT", "no badge"],
)
def test_each_fact_of_a_job_names_the_workload_its_badge_is_for(
    node: Node,
    badge: str | None,
    workload: str | None,
) -> None:
    node.start("job-1", "true", environment={"MERIDIAN_GATEWAY_TOKEN": badge} if badge else {})

    node.wait_until_exited()

    assert [(fact["phase"], fact.get("principal")) for fact in node.facts] == [
        ("start", workload),
        ("exit", workload),
    ]


def test_a_daemons_boot_is_a_fact_of_its_node_that_names_no_job(node: Node) -> None:
    node.process.boot()

    assert node.facts == [{"node": "node-1", "phase": "boot"}]


@pytest.mark.parametrize("badge", [jwt.encode({"sub": "job-0d4c9a1e7b52"}, "a" * 32), None])
def test_a_job_gets_its_own_credentials_and_badge_never_its_nodes_token_and_no_fact_holds_them(
    node: Node,
    badge: str | None,
) -> None:
    secret = "the-jobs-own-secret-on-the-notes-service"
    sent = {"MERIDIAN_NOTES_TOKEN": secret, **({"MERIDIAN_GATEWAY_TOKEN": badge} if badge else {})}
    variables = "$MERIDIAN_NOTES_TOKEN $MERIDIAN_GATEWAY_URL $MERIDIAN_GATEWAY_TOKEN $MERIDIAN_GATEWAY_MODEL"
    variables += " $MERIDIAN_MODELSTORE_URL"
    node.start("job-1", "sh", "-c", f'echo "{variables}"', environment=sent)
    node.wait_until_exited()

    assert node.call("GET", "/output")[1]["stdout"] == f"{secret} {GATEWAY} {badge or ''} {MODEL} {STORE}\n"
    assert secret not in json.dumps(node.facts) and (badge or secret) not in json.dumps(node.facts)


def test_a_job_gets_no_provider_key_from_the_daemons_environment(
    node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "a-provider-key")
    node.start("job-1", "env")
    node.wait_until_exited()

    assert API_KEY_VARIABLE not in node.call("GET", "/output")[1]["stdout"]


@pytest.mark.parametrize(
    ("caller", "token", "refused"),
    [(OTHER_CALLER, TOKEN, HTTPStatus.FORBIDDEN), (TRUSTED_CALLER, "x" * 40, HTTPStatus.UNAUTHORIZED)],
    ids=["another caller holding the token", "the trusted caller holding another token"],
)
def test_the_daemon_serves_its_trusted_caller_alone_holding_its_token(
    node: Node,
    caller: str,
    token: str,
    refused: HTTPStatus,
) -> None:
    client = TestClient(node.app, client=(caller, 50000))
    body = {"job_id": "job-1", "argv": ["true"], "owner": OWNER, "environment": {}}

    response = client.post("/jobs", json=body, headers={"Authorization": f"Bearer {token}"})

    assert (response.status_code, node.facts) == (refused, [])
