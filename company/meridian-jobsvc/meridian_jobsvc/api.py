"""The job service's HTTP API and its MCP tools, over one ``JobService``.

Each operation has the same name in both, as a route's handler and as a tool: ``submit_job``, ``list_jobs``,
``get_job``, ``job_logs``, ``cancel_job``, ``hold_job`` and ``release_job`` on jobs; ``create_schedule``,
``list_schedules``, ``get_schedule`` and ``delete_schedule`` on schedules; ``list_nodes`` and ``usage``; and
the operators' ``operate_job``, ``operate_schedule`` and ``drain_node``. The central authority the deployment
hands in decides every call before it runs and names its caller, never an argument: over HTTP a call without a
known credential is 401 and one without the permission 403, while ``/``, ``/health`` and ``/openapi.json`` are
open. A submitted job, and a created schedule, is issued its own identity from the credential the call
presented. A malformed body is a 422, an unknown job, schedule or node a 404, another's job or schedule a 403,
a job in a state that does not allow the change a 409, a job larger than every node, above the highest
priority or waiting on no job a 422, a submitter the deployment refuses or a job the authority refuses an
identity a 403, and a node or an authority that does not answer a 503; over MCP each refusal is a tool error.
A deployment serves the tools at ``/mcp`` and the API beside them.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import JsonValue, ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from meridian_common.errors import AuthError, MeridianError, NotFoundError
from meridian_jobsvc.authority import CentralAuthority
from meridian_jobsvc.errors import CredentialUnavailable, JobStateConflict
from meridian_jobsvc.fleet import NodeUnavailable
from meridian_jobsvc.models import (
    ClusterNode,
    Command,
    Cpu,
    DependsOn,
    Environment,
    Job,
    JobLogs,
    JobName,
    JobRequest,
    Jobs,
    Labels,
    MemoryMb,
    Nodes,
    Priority,
    Usage,
)
from meridian_jobsvc.schedules import Cron, IntervalSeconds, Schedule, ScheduleRequest, Schedules
from meridian_jobsvc.service import JobService

SERVICE = "jobsvc"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the identities.
ISSUER = "http://jobsvc"
BEARER = "Bearer "
STATUS_OF_REFUSAL = {
    NotFoundError: HTTPStatus.NOT_FOUND,
    AuthError: HTTPStatus.FORBIDDEN,
    JobStateConflict: HTTPStatus.CONFLICT,
    CredentialUnavailable: HTTPStatus.SERVICE_UNAVAILABLE,
}  # any other refusal of the service is a request it can never satisfy
JOB_IDENTITY = (
    "A job runs as an identity of its own (its principal), never as you: it holds what a job may be given of "
    "what you hold when you submit it. Its credential on each service it may call is in the environment "
    "variable MERIDIAN_<SERVICE>_TOKEN (MERIDIAN_NOTES_TOKEN for notes), and the inference gateway's API is "
    "MERIDIAN_GATEWAY_URL, called with MERIDIAN_GATEWAY_TOKEN for the model MERIDIAN_GATEWAY_MODEL names."
)
API_DESCRIPTION = f"""Meridian's cluster: every job runs as a process on a node of its own.
Send your credential as "Authorization: Bearer <credential>". A 403 names the missing permission; ask for it
with POST /requests on the IAM service. POST /jobs with {{"name", "command", "cpu", "memory_mb", "priority",
"depends_on", "labels", "environment"}} submits a job (submit): "command" is the program and its arguments,
run in an empty workspace of the job's own with the variables of "environment", which is on the job's record
and so holds no secret; the checkouts are mounted read-only where they are in a sandbox. {JOB_IDENTITY}
GET /jobs lists every job, GET /jobs/<id> reads one, its principal, state, node, attempt and CPU-hours, and
GET /jobs/<id>/logs the tails of its stdout and stderr (read). DELETE /jobs/<id> cancels a job of your own: it
is cancelling until its process is gone, then cancelled. POST /jobs/<id>/hold keeps a waiting job of yours
from starting, and POST /jobs/<id>/release lets it start again.
POST /schedules with {{"cron" or "interval_seconds", "job"}}, "job" as POST /jobs takes one, submits the job
each time a run is due, every run as one identity of the schedule's own (submit). GET /schedules and
GET /schedules/<id> read schedules and their runs (read), and DELETE /schedules/<id> stops one of yours.
GET /nodes lists the nodes and whether each is free, and GET /usage the CPU-hours each team and each job have
used (read). An operator (operate) cancels, holds or releases any job with
POST /operations/jobs/<id>/<cancel|hold|release>, deletes any schedule with DELETE /operations/schedules/<id>,
and drains a node with POST /nodes/<id>/drain: it starts no new job, and its job runs again elsewhere, as its
next attempt, once a grace period is over. The schema is at /openapi.json.
"""
SUBMIT_DESCRIPTION = (
    "Submit a job to Meridian's cluster. Its command runs as a process on a node of its own, in an empty "
    "workspace, with the checkouts mounted read-only where they are in your sandbox and the variables of "
    "environment, which is on the job's record and so holds no secret; it starts once the jobs it depends on "
    "have succeeded and a node with its cpus and memory is free, and runs until it exits. "
    f"{JOB_IDENTITY}"
)
SCHEDULE_DESCRIPTION = (
    "Create a schedule: the job, as submit_job takes one, is submitted each time a run is due, by cron (five "
    "fields, in UTC) or every interval_seconds; a due time that finds the last run unfinished is skipped. "
    "Every run runs as one identity of the schedule's own, issued now."
)


class JobAction(StrEnum):
    """What an operator does to a job, as a job's owner may to their own."""

    CANCEL = "cancel"
    HOLD = "hold"
    RELEASE = "release"


def create_jobsvc_api(
    service: JobService,
    authority: CentralAuthority,
    write_record: Callable[[str], None],
) -> FastAPI:
    """The HTTP API over ``service``; ``authority`` decides every call before its handler runs.

    Each handler names its operation and arguments on ``request.state``, and ``write_record`` writes one
    audit line per request: its caller, operation, arguments, status and the IAM decision it was made under.
    """
    app = FastAPI(
        title="Meridian job service",
        docs_url=None,
        redoc_url=None,
        dependencies=list(authority.dependencies),
    )

    @app.middleware("http")
    async def record_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Write one audit line per request answered, a refused one included: the company's own record.

        The operation and caller fall back to the ones the authority sets before a handler runs, so a call
        refused before its handler still names what was asked and by whom.
        """
        response = await call_next(request)
        operation = getattr(request.state, "operation", None) or getattr(request.state, "iam_operation", None)
        arguments = getattr(request.state, "arguments", dict(request.path_params))
        caller = getattr(request.state, "iam_principal", None)
        decision = getattr(request.state, "iam_decision_id", None)
        line = {
            "wall_ts": datetime.now(UTC).isoformat(),
            "caller": caller,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            **({"decision": decision} if decision else {}),
            **({"operation": operation, "arguments": arguments} if operation else {}),
        }
        write_record(json.dumps(line))
        return response

    def name_call(request: Request, arguments: Mapping[str, JsonValue]) -> str:
        """Name the route's operation and its arguments; returns the caller the authority named."""
        request.state.operation = request.scope["route"].name
        request.state.arguments = dict(arguments)
        return getattr(request.state, "iam_principal", None) or ""

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """The authority decides even a body too malformed to read: its refusal stands, else the 422."""
        try:
            for decide in authority.dependencies:
                if decide.dependency is not None:
                    await decide.dependency(request)
        except HTTPException as refused:
            return await http_exception_handler(request, refused)
        name_call(request, request.path_params)
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(MeridianError)
    async def refuse(_: Request, error: MeridianError) -> Response:
        status = next(
            (status for kind, status in STATUS_OF_REFUSAL.items() if isinstance(error, kind)),
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
        return JSONResponse({"detail": error.message}, status)

    @app.exception_handler(NodeUnavailable)
    async def answer_unavailable(_: Request, error: NodeUnavailable) -> Response:
        return JSONResponse({"detail": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.post("/jobs", status_code=HTTPStatus.CREATED)
    def submit_job(request: Request, job: JobRequest) -> Job:
        """Not async: the job's identity is issued by the central authority before it is queued."""
        return service.submit(job, name_call(request, job.model_dump()), presented_credential(request))

    @app.get("/jobs")
    async def list_jobs(request: Request) -> Jobs:
        name_call(request, {})
        return Jobs(jobs=service.list_jobs())

    @app.get("/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> Job:
        name_call(request, {"job_id": job_id})
        return service.get_job(job_id)

    @app.get("/jobs/{job_id}/logs")
    def job_logs(request: Request, job_id: str) -> JobLogs:
        """Not async: a running job's output is read from its node."""
        name_call(request, {"job_id": job_id})
        return service.logs(job_id)

    @app.delete("/jobs/{job_id}", status_code=HTTPStatus.ACCEPTED)
    async def cancel_job(request: Request, job_id: str) -> Job:
        return service.cancel(job_id, name_call(request, {"job_id": job_id}))

    @app.post("/jobs/{job_id}/hold")
    async def hold_job(request: Request, job_id: str) -> Job:
        return service.hold(job_id, name_call(request, {"job_id": job_id}))

    @app.post("/jobs/{job_id}/release")
    async def release_job(request: Request, job_id: str) -> Job:
        return service.release(job_id, name_call(request, {"job_id": job_id}))

    @app.post("/schedules", status_code=HTTPStatus.CREATED)
    def create_schedule(request: Request, schedule: ScheduleRequest) -> Schedule:
        """Not async: the schedule's identity is issued by the central authority before it is kept."""
        owner = name_call(request, schedule.model_dump())
        return service.create_schedule(schedule, owner, presented_credential(request))

    @app.get("/schedules")
    async def list_schedules(request: Request) -> Schedules:
        name_call(request, {})
        return Schedules(schedules=service.list_schedules())

    @app.get("/schedules/{schedule_id}")
    async def get_schedule(request: Request, schedule_id: str) -> Schedule:
        name_call(request, {"schedule_id": schedule_id})
        return service.get_schedule(schedule_id)

    @app.delete("/schedules/{schedule_id}")
    async def delete_schedule(request: Request, schedule_id: str) -> Schedule:
        return service.delete_schedule(schedule_id, name_call(request, {"schedule_id": schedule_id}))

    @app.post("/operations/jobs/{job_id}/{action}")
    async def operate_job(request: Request, job_id: str, action: JobAction, response: Response) -> Job:
        """A cancel is accepted as an owner's is: the job is cancelling until its process is gone."""
        name_call(request, {"job_id": job_id, "action": action.value})
        if action is JobAction.CANCEL:
            response.status_code = HTTPStatus.ACCEPTED
        return operate(service, job_id, action)

    @app.delete("/operations/schedules/{schedule_id}")
    async def operate_schedule(request: Request, schedule_id: str) -> Schedule:
        name_call(request, {"schedule_id": schedule_id})
        return service.delete_schedule(schedule_id, None)

    @app.get("/nodes")
    async def list_nodes(request: Request) -> Nodes:
        name_call(request, {})
        return Nodes(nodes=service.list_nodes())

    @app.post("/nodes/{node_id}/drain", status_code=HTTPStatus.ACCEPTED)
    async def drain_node(request: Request, node_id: str) -> ClusterNode:
        name_call(request, {"node_id": node_id})
        return service.drain(node_id)

    @app.get("/usage")
    async def usage(request: Request) -> Usage:
        name_call(request, {})
        return service.usage()

    return app


def build_jobsvc_server(service: JobService, authority: CentralAuthority) -> MCPServer:
    """The MCP server of the job service: one tool per operation of the HTTP API, with the same name."""
    server = MCPServer(
        SERVICE,
        token_verifier=authority.verifier,
        auth=AuthSettings(issuer_url=ISSUER, resource_server_url=None),
        middleware=list(authority.middleware),
        log_level="WARNING",
    )

    def require_caller() -> str:
        caller = authority.caller()
        if caller is None:
            raise ToolError("no identity: the job service serves only known callers")
        return caller

    # Not async: the SDK runs it in a worker thread, as the job's identity is issued before it is queued.
    @server.tool(description=SUBMIT_DESCRIPTION)
    def submit_job(  # noqa: PLR0913 - every field of a job
        name: JobName,
        command: Command,
        cpu: Cpu = 1.0,
        memory_mb: MemoryMb = 512,
        priority: Priority = 0,
        depends_on: DependsOn | None = None,
        labels: Labels | None = None,
        environment: Environment | None = None,
    ) -> Job:
        with refusals_as_tool_errors():
            job = JobRequest(
                name=name,
                command=command,
                cpu=cpu,
                memory_mb=memory_mb,
                priority=priority,
                depends_on=depends_on or [],
                labels=labels or {},
                environment=environment or {},
            )
            return service.submit(job, require_caller(), presented_token())

    @server.tool(description="List every job of the cluster, oldest first, with its state and node.")
    async def list_jobs() -> Jobs:
        require_caller()
        return Jobs(jobs=service.list_jobs())

    @server.tool(
        description="Read one job: what it runs, its state, node and attempt, its CPU-hours, how it ended.",
    )
    async def get_job(job_id: str) -> Job:
        require_caller()
        with refusals_as_tool_errors():
            return service.get_job(job_id)

    # Not async: the SDK runs it in a worker thread, as a running job's output is read from its node.
    @server.tool(description="The tails of a job's stdout and stderr, read from its node while it runs.")
    def job_logs(job_id: str) -> JobLogs:
        require_caller()
        with refusals_as_tool_errors():
            return service.logs(job_id)

    @server.tool(
        description="Cancel a job of your own: it is cancelling until its process is gone, then cancelled.",
    )
    async def cancel_job(job_id: str) -> Job:
        with refusals_as_tool_errors():
            return service.cancel(job_id, require_caller())

    @server.tool(description="Keep a waiting job of your own from starting, until you release it.")
    async def hold_job(job_id: str) -> Job:
        with refusals_as_tool_errors():
            return service.hold(job_id, require_caller())

    @server.tool(description="Let a held job of your own start again, in the place it had in the queue.")
    async def release_job(job_id: str) -> Job:
        with refusals_as_tool_errors():
            return service.release(job_id, require_caller())

    # Not async: the SDK runs it in a worker thread, as the schedule's identity is issued before it is kept.
    @server.tool(description=SCHEDULE_DESCRIPTION)
    def create_schedule(
        job: JobRequest,
        cron: Cron | None = None,
        interval_seconds: IntervalSeconds | None = None,
    ) -> Schedule:
        with refusals_as_tool_errors():
            schedule = ScheduleRequest(job=job, cron=cron, interval_seconds=interval_seconds)
            return service.create_schedule(schedule, require_caller(), presented_token())

    @server.tool(description="List every schedule of the cluster, deleted ones included, with their runs.")
    async def list_schedules() -> Schedules:
        require_caller()
        return Schedules(schedules=service.list_schedules())

    @server.tool(
        description="Read one schedule: its job, when it runs, its next run and the runs it started.",
    )
    async def get_schedule(schedule_id: str) -> Schedule:
        require_caller()
        with refusals_as_tool_errors():
            return service.get_schedule(schedule_id)

    @server.tool(description="Delete a schedule of your own: it starts no more runs; those started carry on.")
    async def delete_schedule(schedule_id: str) -> Schedule:
        with refusals_as_tool_errors():
            return service.delete_schedule(schedule_id, require_caller())

    @server.tool(description="As an operator, cancel, hold or release any job of the cluster.")
    async def operate_job(job_id: str, action: JobAction) -> Job:
        require_caller()
        with refusals_as_tool_errors():
            return operate(service, job_id, action)

    @server.tool(description="As an operator, delete any schedule of the cluster: it starts no more runs.")
    async def operate_schedule(schedule_id: str) -> Schedule:
        require_caller()
        with refusals_as_tool_errors():
            return service.delete_schedule(schedule_id, None)

    @server.tool(
        description="List the cluster's nodes: the cpus and memory a job on each gets, and its state.",
    )
    async def list_nodes() -> Nodes:
        require_caller()
        return Nodes(nodes=service.list_nodes())

    @server.tool(
        description="As an operator, drain a node: it starts no new job, and its job runs again elsewhere, "
        "as its next attempt, once a grace period is over.",
    )
    async def drain_node(node_id: str) -> ClusterNode:
        require_caller()
        with refusals_as_tool_errors():
            return service.drain(node_id)

    @server.tool(
        description="The CPU-hours each team's jobs and each job have used, from the wall time they ran.",
    )
    async def usage() -> Usage:
        require_caller()
        return service.usage()

    return server


def operate(service: JobService, job_id: str, action: JobAction) -> Job:
    """An operator's ``action`` on any job: the authority allowed it, so it is no one's own."""
    change = {
        JobAction.CANCEL: service.cancel,
        JobAction.HOLD: service.hold,
        JobAction.RELEASE: service.release,
    }
    return change[action](job_id, None)


def presented_credential(request: Request) -> str | None:
    """The credential an HTTP call presented, which a job or a schedule it launches gets its identity from."""
    header = request.headers.get("Authorization", "")
    return header.removeprefix(BEARER) if header.startswith(BEARER) else None


def presented_token() -> str | None:
    """The credential the tool call being served presented, for the same."""
    presented = get_access_token()
    return None if presented is None else presented.token


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise the service's refusal as a tool error: only a tool error's text reaches the caller."""
    try:
        yield
    except MeridianError as error:
        raise ToolError(error.message) from error
    except (NodeUnavailable, ValidationError) as error:
        raise ToolError(str(error)) from error
