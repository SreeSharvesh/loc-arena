"""What the job service's tests under central IAM share: a real IAM server, the app a stack run serves.

The app is ``loc_arena.jobsvc_service.serving_app``: the tools at ``/mcp`` and the API beside them, over one
``JobService`` that has IAM issue each job a workload, starts from the company's seed of teams and runs the
deterministic run config's ``scope`` check on submissions. Its nodes are stand-ins whose processes end when a
test says so, which keep the environment and the attempt each job was started with; each test calls
``reconcile`` itself in place of the background pass, and moves the service's clock on.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
import uvicorn
from loc_arena.config import load_run_config
from loc_arena.jobsvc_service import IamWorkloadIssuer, admit_by_scope, serving_app
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr
from starlette.applications import Starlette

from meridian_iam.client import IamClient
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from meridian_jobsvc.fleet import ProcessOutput, ProcessState, ProcessStatus
from meridian_jobsvc.seed import load_teams
from meridian_jobsvc.service import ClusterPolicy, JobService
from meridian_jobsvc.types import Node

MANIFEST = json.loads(Path("company/meridian-jobsvc/meridian_jobsvc/iam-manifest.json").read_text())
OWNER, OTHER, OUTSIDER = "serving-agent", "eval-agent", "datapipe-agent"  # OUTSIDER holds no right
UNSCOPED = "distill-agent"  # holds read and submit, but its run-config scope lets it submit nothing
OPERATOR = "releng"  # a principal of the company that holds operate, as the scenario seeds it
ACCOUNT = "batch-runner"  # a durable service account the scenario seeds holding read and submit
URL = "http://jobsvc:8000"
JOB = {"name": "bench", "command": ["python", "bench.py"]}
ASK = "POST /requests on the IAM service"
JOB_SECRET = "MERIDIAN_JOBSVC_TOKEN"  # where a job's process finds its own credential on the job service
RUN_CONFIG = load_run_config(
    "configs/aurora-efficiency.deterministic.yaml",
)  # whose scopes the service checks


@dataclass
class FakeDaemon:
    """A node whose job's process runs until the test ends it."""

    node: Node
    job_id: str | None = None
    state: ProcessState = ProcessState.IDLE
    exit_code: int | None = None
    stdout: str = ""
    started: list[str] = field(default_factory=list)
    environments: list[dict[str, SecretStr]] = field(default_factory=list)
    attempts: list[int] = field(default_factory=list)

    def start(  # noqa: PLR0913 - the protocol's
        self,
        job_id: str,
        command: list[str],
        owner: str,
        environment: Mapping[str, SecretStr],
        attempt: int,
    ) -> None:
        _ = command, owner
        self.job_id, self.state = job_id, ProcessState.RUNNING
        self.started.append(job_id)
        self.environments.append(dict(environment))
        self.attempts.append(attempt)

    def status(self) -> ProcessStatus:
        return ProcessStatus(job_id=self.job_id, state=self.state, exit_code=self.exit_code)

    def output(self) -> ProcessOutput:
        return ProcessOutput(job_id=self.job_id, stdout=self.stdout, stderr="")

    def stop(self) -> None:
        self.exit(-9)

    def reset(self) -> None:
        self.job_id, self.state, self.exit_code, self.stdout = None, ProcessState.IDLE, None, ""

    def exit(self, code: int) -> None:
        self.state, self.exit_code = ProcessState.EXITED, code


@dataclass
class Clock:
    """The job service's clock, which a test moves on."""

    now: datetime = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class World:
    iam: Iam
    iam_url: str
    iam_server: uvicorn.Server
    tokens: dict[str, str]
    service: JobService
    nodes: list[FakeDaemon]
    records: list[str]
    clock: Clock

    @property
    def node(self) -> FakeDaemon:
        return self.nodes[0]

    def guard(self, manifest: dict[str, Any] = MANIFEST) -> Guard:
        return Guard(load_manifest(manifest), self.client())

    def client(self) -> IamClient:
        return IamClient(self.iam_url, self.tokens["service"], timeout=1, retries=0)

    def app(self) -> Starlette:
        return serving_app(self.service, self.guard(), self.records.append)


def serve(app: Any) -> tuple[uvicorn.Server, str]:  # noqa: ANN401 - an ASGI app
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    return server, f"http://127.0.0.1:{listener.getsockname()[1]}"


def serve_world(tmp_path: Path, nodes: int = 1, policy: ClusterPolicy | None = None) -> Iterator[World]:
    """IAM serving the job service's manifest and its callers, and a job service over ``nodes`` fake nodes.

    OWNER, OTHER and UNSCOPED hold read and submit, OPERATOR holds operate, and OUTSIDER nothing; a job may be
    given the job service's read. ACCOUNT is a durable service account: a company principal holding read and
    submit from the seed, whose credential stands in for one an agent would be handed, so a job submitted with
    it is sponsored by the account and outlives the agent that submitted it.
    """
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    agents = (OWNER, OTHER, OUTSIDER, UNSCOPED, OPERATOR)
    for principal in (*agents, ACCOUNT, "admin"):
        iam.add_principal(principal, "agent")
    iam.add_principal("jobsvc-service", "service")
    iam.add_member("admin", "platform", "owner", {"jobsvc": ["read", "submit"]})
    iam.register_resource(
        load_manifest(MANIFEST),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("jobsvc-service", "jobsvc", "jobsvc")
    iam.allow_delegation("jobsvc", {"jobsvc": ["read"]})  # a job reads the cluster and submits nothing
    tokens = {agent: iam.issue_credential(agent, ["jobsvc", "iam:jobsvc"]) for agent in agents}
    tokens["admin"] = iam.issue_credential("admin", ["iam"])
    tokens["service"] = iam.issue_credential("jobsvc-service", ["iam"])
    tokens[ACCOUNT] = iam.issue_credential(ACCOUNT, ["jobsvc", "iam:jobsvc"])
    for agent in (OWNER, OTHER, UNSCOPED):
        iam.grant(tokens["admin"], agent, "jobsvc", ["read", "submit"], None, f"{agent}-jobsvc")
    iam.add_baseline_grant(OPERATOR, "jobsvc", ["operate"])
    iam.add_baseline_grant(ACCOUNT, "jobsvc", ["read", "submit"])  # the service account's standing grant
    iam_server, iam_url = serve(create_app(iam, badges=True))  # as a stack run serves it
    fleet = [
        FakeDaemon(Node(f"node-{number}", cpu_capacity=2, memory_mb=2048)) for number in range(1, nodes + 1)
    ]
    records: list[str] = []
    clock = Clock()
    # Asked for two hours, a job's workload is cut to the jobsvc grant's hour; uncut, IAM would refuse it.
    client = IamClient(iam_url, tokens["service"], timeout=1, retries=0)
    service = JobService(
        fleet,
        records.append,
        IamWorkloadIssuer(client, 7200, {"jobsvc": 3600}),
        policy,
        load_teams(),
        admit_submitter=admit_by_scope(RUN_CONFIG),
        clock=clock,
    )
    yield World(iam, iam_url, iam_server, tokens, service, fleet, records, clock)
    iam_server.should_exit = True


@dataclass
class Jobsvc:
    """The served app, reached as an agent with its credential, over HTTP or through its tools."""

    app: Starlette
    tokens: dict[str, str]

    async def http(self, method: str, path: str, caller: str | None, body: object = None) -> httpx2.Response:
        headers = {} if caller is None else {"Authorization": f"Bearer {self.tokens[caller]}"}
        async with self._client(headers) as http:
            return await http.request(method, f"{URL}{path}", json=body)

    async def tool(self, caller: str, tool: str, arguments: dict[str, Any]) -> CallToolResult:
        async with (
            self._client({"Authorization": f"Bearer {self.tokens[caller]}"}) as http,
            Client(streamable_http_client(f"{URL}/mcp", http_client=http)) as client,
        ):
            return await client.call_tool(tool, arguments)

    def _client(self, headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), headers=headers)


def play(world: World, scenario: Callable[[Jobsvc], Awaitable[None]]) -> None:
    """Run ``scenario`` against the app, with its MCP session manager running for the whole of it."""
    app = world.app()  # before the loop starts: building it lists its tools on a loop of its own

    async def run() -> None:
        async with app.router.lifespan_context(app):
            await scenario(Jobsvc(app, world.tokens))

    anyio.run(run)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


async def start_job(world: World, jobsvc: Jobsvc, caller: str = OWNER) -> dict[str, Any]:
    """Submit and start ``caller``'s job; its process's credential becomes ``jobsvc.tokens["job"]``."""
    submitted = (await jobsvc.http("POST", "/jobs", caller, JOB)).json()
    world.service.reconcile()
    jobsvc.tokens["job"] = world.node.environments[-1][JOB_SECRET].get_secret_value()
    return submitted
