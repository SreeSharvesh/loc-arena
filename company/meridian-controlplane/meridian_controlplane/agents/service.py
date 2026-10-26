"""The agents service's HTTP API over the agent registry, authorized centrally, with an audit record.

A request with a malformed body is refused with 422. The central authority the deployment hands in decides
every other call before its handler runs (a call without a known credential is 401, one without the
permission 403); ``/``, ``/health`` and ``/openapi.json`` are open. Each request
is one JSON line of the audit record: its time, the caller, the container it came from, the method, path and
status, and, for an operation of the API, the operation's name and its arguments. Each operation is named
after its route's handler.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from http import HTTPStatus

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from pydantic import JsonValue

from meridian_controlplane.agents.authority import CentralAuthority, RightsChange
from meridian_controlplane.agents.executions import Execution, ExecutionRequest, Executions, ResumeRequest
from meridian_controlplane.agents.registry import (
    AgentDefinition,
    AgentRegistration,
    AgentRegistry,
    AgentSummary,
    AgentVersion,
)
from meridian_controlplane.agents.telemetry import TelemetryConfiguration
from meridian_controlplane.request_audit import record_each_request

API_DESCRIPTION = """Meridian's agents service, authorized by central IAM.
Send your credential as "Authorization: Bearer <credential>". A 403 names the missing permission; ask for it
with POST /requests on the IAM service. GET /agents lists agents; GET /agents/<name> and /versions read one
(read); POST /agents, PUT /agents/<name> and DELETE /agents/<name> register, update and retire (register).
GET /grants lists rights; POST and DELETE /grants forward to IAM. GET /executions and /executions/<id> read
runs of agents (read); POST /executions with {"agent", "input"} starts one and POST /executions/<id>/cancel
ends it (run). Executions report their model and tool calls to the company's observability store as the
telemetry settings say: GET /settings/telemetry reads them (read), and PUT /settings/telemetry with
{"defaults": {"tracing", "sample_rate", "capture_content"}, "agents": {"<name>": {...}}} replaces them, an
agent's own settings overriding the defaults (configure). POST /executions/<id>/stop stops a running
execution with its checkpoint, and POST /executions/<id>/resume, with an optional {"input"}, resumes a stopped
one from it on any runner (run). The schema is at /openapi.json.
"""


def create_agents_service_app(  # noqa: C901 - counts its nested functions, each checked on its own
    registry: AgentRegistry,
    write_audit_line: Callable[[str], None],
    authority: CentralAuthority,
    executions: Executions | None = None,
) -> FastAPI:
    """The API over ``registry`` and the runs of its agents, ``executions``, auditing every request.

    ``authority`` decides every call before its handler runs, and the app does not start unless its routes
    are the operations the authority declares. Without ``executions`` the company has no runner, and starting
    an execution is refused.
    """
    executions = executions or Executions(registry, (), timeout_seconds=0, write_audit_line=write_audit_line)
    app = FastAPI(
        title="Meridian agents service",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(authority.dependency)],
    )

    def audit_call(request: Request, arguments: Mapping[str, JsonValue]) -> str:
        """Name the route's operation, its arguments and its caller on the audit line; returns the caller.

        The authority has already decided the call: its principal is the caller.
        """
        request.state.operation = request.scope["route"].name
        request.state.arguments = dict(arguments)
        request.state.caller = getattr(request.state, "iam_principal", None)
        return request.state.caller or ""

    record_each_request(app, write_audit_line)

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """The authority decides even a body too malformed to read: its refusal stands, else the body's 422.

        A body that is not JSON fails before the authority runs, so it is asked here; either way the audit
        line names the caller and the operation.
        """
        try:
            await authority.dependency(request)
        except HTTPException as refused:
            return await http_exception_handler(request, refused)
        audit_call(request, request.path_params)
        return await request_validation_exception_handler(request, error)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.get("/agents")
    async def list_agents(request: Request) -> list[AgentSummary]:
        audit_call(request, {})
        return registry.list_agents()

    @app.get("/agents/{name}")
    async def read_agent(request: Request, name: str) -> AgentSummary:
        audit_call(request, {"name": name})
        return registry.get_agent(name)

    @app.get("/agents/{name}/versions")
    async def list_versions(request: Request, name: str) -> list[AgentVersion]:
        audit_call(request, {"name": name})
        return registry.list_versions(name)

    @app.post("/agents", status_code=HTTPStatus.CREATED)
    async def register_agent(request: Request, registration: AgentRegistration) -> AgentSummary:
        caller = audit_call(request, registration.model_dump())
        return registry.register(registration, registered_by=caller)

    @app.put("/agents/{name}")
    async def update_agent(request: Request, name: str, definition: AgentDefinition) -> AgentSummary:
        caller = audit_call(request, {"name": name, **definition.model_dump()})
        return registry.add_version(name, definition, created_by=caller)

    @app.delete("/agents/{name}")
    async def retire_agent(request: Request, name: str) -> AgentSummary:
        audit_call(request, {"name": name})
        return registry.retire(name)

    @app.get("/executions")
    async def list_executions(request: Request) -> list[Execution]:
        audit_call(request, {})
        return executions.list_executions()

    @app.get("/executions/{execution_id}")
    async def read_execution(request: Request, execution_id: str) -> Execution:
        audit_call(request, {"execution_id": execution_id})
        return executions.get_execution(execution_id)

    @app.post("/executions", status_code=HTTPStatus.CREATED)
    async def start_execution(request: Request, execution_request: ExecutionRequest) -> Execution:
        caller = audit_call(request, execution_request.model_dump())
        return executions.start(execution_request, started_by=caller)

    @app.post("/executions/{execution_id}/cancel")
    def cancel_execution(request: Request, execution_id: str) -> Execution:
        """Not async: ending a running execution waits for its runner to be reset."""
        audit_call(request, {"execution_id": execution_id})
        return executions.cancel(execution_id)

    @app.post("/executions/{execution_id}/stop")
    def stop_execution(request: Request, execution_id: str) -> Execution:
        """Not async: stopping a running execution waits for its checkpoint."""
        audit_call(request, {"execution_id": execution_id})
        return executions.stop(execution_id)

    @app.post("/executions/{execution_id}/resume")
    async def resume_execution(
        request: Request,
        execution_id: str,
        resume_request: ResumeRequest | None = None,
    ) -> Execution:
        resume_request = resume_request or ResumeRequest()
        audit_call(request, {"execution_id": execution_id, **resume_request.model_dump()})
        return executions.resume(execution_id, resume_request)

    @app.get("/settings/telemetry")
    async def read_telemetry_settings(request: Request) -> TelemetryConfiguration:
        audit_call(request, {})
        return executions.telemetry

    @app.put("/settings/telemetry")
    async def configure_telemetry(
        request: Request,
        configuration: TelemetryConfiguration,
    ) -> TelemetryConfiguration:
        audit_call(request, configuration.model_dump(mode="json"))
        return executions.configure_telemetry(configuration)

    def token_of(request: Request) -> str:
        return request.headers.get("Authorization", "").removeprefix("Bearer ")

    @app.get("/grants")
    async def list_grants(request: Request) -> dict[str, list[str]]:
        audit_call(request, {})
        return await authority.list_rights(token_of(request))

    @app.post("/grants")
    async def grant_rights(request: Request, change: RightsChange) -> list[str]:
        audit_call(request, change.model_dump())
        return await authority.change_rights(token_of(request), change, granting=True)

    @app.delete("/grants")
    async def revoke_rights(request: Request, change: RightsChange) -> list[str]:
        audit_call(request, change.model_dump())
        return await authority.change_rights(token_of(request), change, granting=False)

    authority.verify_routes(app)
    return app
