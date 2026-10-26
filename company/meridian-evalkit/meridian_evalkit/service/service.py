"""The eval service: its HTTP API and its MCP tools, over one ``EvalkitRegistry``, authorized centrally.

Each operation has one name, as an HTTP route's handler, as an MCP tool and in the IAM manifest, which binds
it to the permission it needs: ``read`` to list and inspect evals, runs, diffs and leaderboards, ``run`` to
start a run, ``curate`` to register and retire evals, ``publish`` to put a run on its eval's leaderboard. Two
are a run's job's alone and have no tool: the job binds itself to its run (``POST /runs/{run_id}/start``) and
reports its results (``POST /runs/{run_id}/results``); any valid credential reaches them, and the registry
refuses every caller but the run's job. The central authority a deployment hands in decides every call before
its handler runs (no known credential is a 401, a missing permission a 403); a malformed request is a 422; and
each request is one line of the audit record (``meridian_controlplane.request_audit``). ``/``, ``/health`` and
``/openapi.json`` are open. Over MCP the deployment's middleware decides each call, and a refused call is a
tool error. A deployment serves the tools at ``/mcp`` and the API beside them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, JsonValue, ValidationError

from meridian_controlplane.request_audit import record_each_request
from meridian_evalkit.service.authority import Authority
from meridian_evalkit.service.registry import (
    EVAL_ID_PATTERN,
    Board,
    Diff,
    Eval,
    EvalkitRegistry,
    EvalRegistration,
    EvalServiceError,
    EvalSummary,
    Results,
    Run,
    RunStart,
    TextItem,
)

SERVICE = "evalkit"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the credentials.
ISSUER = "http://evalkit"

API_DESCRIPTION = """Meridian's eval service: the evals, the runs on them, their results and the leaderboards.
Send your credential as "Authorization: Bearer <credential>". A 403 names the missing permission; ask for it
with POST /requests on the IAM service.
GET /evals lists the evals and GET /evals/<eval_id> reads one with its items (read). POST /evals with
{"eval_id", "items": [{"item_id", "prompt", "reference", "category"}], "metrics"} registers one; the first
metric is its headline, the score its leaderboard ranks by. DELETE /evals/<eval_id> retires it (curate).
POST /runs with {"eval_id", "model_id"} starts a run (run): the answer carries the job to submit to the
cluster, which runs the eval and reports back. GET /runs lists the runs, GET /runs/<run_id> reads one and
GET /runs/<a>/diff/<b> gives the metric deltas between two reported runs (read).
POST /runs/<run_id>/publish puts a reported run on its eval's leaderboard (publish); GET /leaderboard, with
?eval_id=<eval_id> for one eval, ranks the published runs by the headline metric, best first (read).
A run's job alone calls POST /runs/<run_id>/start, which binds it to the run, and POST /runs/<run_id>/results
with {"metrics": exactly the eval's, "item_ids": the eval's items it scored}, which reports. The schema is at
/openapi.json.
"""


class Evals(BaseModel):
    """Every eval."""

    evals: list[EvalSummary]


class Runs(BaseModel):
    """Every run."""

    runs: list[Run]


class Boards(BaseModel):
    """The leaderboards."""

    boards: list[Board]


def create_evalkit_api(  # noqa: C901 - counts its nested functions, each checked on its own
    registry: EvalkitRegistry,
    write_audit_line: Callable[[str], None],
    authority: Authority,
) -> FastAPI:
    """The HTTP API over ``registry``, auditing every request.

    ``authority`` decides every call before its handler runs and names the caller as
    ``request.state.iam_principal``; the app does not start unless its routes are the operations the
    authority declares.
    """
    app = FastAPI(
        title="Meridian eval service",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(authority.dependency)],
    )
    record_each_request(app, write_audit_line)

    def name_call(request: Request, arguments: Mapping[str, JsonValue]) -> str | None:
        """Name the route's operation and its arguments on the audit line; the caller if the authority ran."""
        request.state.operation = request.scope["route"].name
        request.state.arguments = dict(arguments)
        request.state.caller = getattr(request.state, "iam_principal", None)
        return request.state.caller

    def audit_call(request: Request, arguments: Mapping[str, JsonValue]) -> str:
        """``name_call`` on a route, where the authority has decided the call and named its caller."""
        caller = name_call(request, arguments)
        if caller is None:
            raise RuntimeError(f"the authority named no caller for {request.state.operation}")
        return caller

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """Refused before the route runs, a malformed request's audit line still names the operation.

        A body that is not JSON is refused before the authority runs, so that line may name no caller. The
        answer says where and why, never echoing the input, which may not be JSON (a NaN metric is not).
        """
        name_call(request, request.path_params)
        wrong = [{key: found[key] for key in ("loc", "msg", "type")} for found in error.errors()]
        return JSONResponse({"detail": wrong}, HTTPStatus.UNPROCESSABLE_ENTITY)

    @app.exception_handler(EvalServiceError)
    async def refuse(_: Request, error: EvalServiceError) -> Response:
        return JSONResponse({"detail": str(error)}, error.status)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.get("/evals")
    async def list_evals(request: Request) -> Evals:
        audit_call(request, {})
        return Evals(evals=registry.list_evals())

    @app.get("/evals/{eval_id}")
    async def get_eval(request: Request, eval_id: str) -> Eval:
        audit_call(request, {"eval_id": eval_id})
        return registry.get_eval(eval_id)

    @app.post("/evals", status_code=HTTPStatus.CREATED)
    async def register_eval(request: Request, registration: EvalRegistration) -> Eval:
        caller = audit_call(request, registration.model_dump())
        return registry.register_eval(caller, registration)

    @app.delete("/evals/{eval_id}")
    async def retire_eval(request: Request, eval_id: str) -> Eval:
        audit_call(request, {"eval_id": eval_id})
        return registry.retire_eval(eval_id)

    @app.post("/runs", status_code=HTTPStatus.CREATED)
    async def start_run(request: Request, start: RunStart) -> Run:
        caller = audit_call(request, start.model_dump())
        return registry.start_run(caller, start)

    @app.get("/runs")
    async def list_runs(request: Request) -> Runs:
        audit_call(request, {})
        return Runs(runs=registry.list_runs())

    @app.get("/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> Run:
        audit_call(request, {"run_id": run_id})
        return registry.get_run(run_id)

    @app.post("/runs/{run_id}/start")
    async def bind_job(request: Request, run_id: str) -> Run:
        principal = audit_call(request, {"run_id": run_id})
        return registry.bind_job(run_id, principal)

    @app.post("/runs/{run_id}/results", status_code=HTTPStatus.CREATED)
    async def report_results(request: Request, run_id: str, results: Results) -> Run:
        principal = audit_call(request, {"run_id": run_id, **results.model_dump()})
        return registry.report_results(run_id, principal, results)

    @app.get("/runs/{left}/diff/{right}")
    async def diff_runs(request: Request, left: str, right: str) -> Diff:
        audit_call(request, {"left": left, "right": right})
        return registry.diff_runs(left, right)

    @app.post("/runs/{run_id}/publish")
    async def publish_run(request: Request, run_id: str) -> Run:
        audit_call(request, {"run_id": run_id})
        return registry.publish_run(run_id)

    @app.get("/leaderboard")
    async def leaderboard(request: Request, eval_id: Annotated[str | None, Query()] = None) -> Boards:
        audit_call(request, {} if eval_id is None else {"eval_id": eval_id})
        return Boards(boards=registry.leaderboards(eval_id))

    authority.verify_routes(app)
    return app


def build_evalkit_server(  # noqa: C901 - counts its nested functions, each checked on its own
    registry: EvalkitRegistry,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of the eval service: one tool per operation agents call, with the same name.

    ``caller`` names who calls and ``verifier`` admits callers over HTTP. ``middleware`` runs in order around
    every request: a deployment records each call there and, under central IAM, decides it before the tool
    runs; a tool itself needs only a caller.
    """
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
    server = MCPServer(
        SERVICE,
        token_verifier=verifier,
        auth=auth,
        middleware=list(middleware),
        log_level="WARNING",
    )

    def require_caller() -> str:
        agent = caller()
        if agent is None:
            raise ToolError("no identity: the eval service serves only known callers")
        return agent

    # Async, so every call runs on the event loop, one at a time, as the HTTP handlers do.
    @server.tool(description="List the evals: each one's id, metrics and item count, retired ones too.")
    async def list_evals() -> Evals:
        require_caller()
        return Evals(evals=registry.list_evals())

    @server.tool(description="Read an eval in full: its items, and the metrics its runs report.")
    async def get_eval(
        eval_id: Annotated[str, Field(description="the eval's id, e.g. general-knowledge")],
    ) -> Eval:
        require_caller()
        with refusals_as_tool_errors():
            return registry.get_eval(eval_id)

    @server.tool(
        description="Register an eval: its items and the metrics its runs report. The first metric is its "
        "headline, the score its leaderboard ranks by.",
    )
    async def register_eval(
        eval_id: Annotated[str, Field(pattern=EVAL_ID_PATTERN, description="a new id: lowercase, digits, -")],
        items: Annotated[
            list[TextItem],
            Field(description="each an id, a prompt, its reference answer and a category"),
        ],
        metrics: Annotated[
            list[str],
            Field(description="e.g. accuracy, ece, brier; the first is the headline"),
        ],
    ) -> Eval:
        agent = require_caller()
        with refusals_as_tool_errors():
            registration = EvalRegistration(eval_id=eval_id, items=items, metrics=metrics)
            return registry.register_eval(agent, registration)

    @server.tool(description="Retire an eval: no new runs on it; its runs and results stay.")
    async def retire_eval(eval_id: Annotated[str, Field(description="the eval's id")]) -> Eval:
        require_caller()
        with refusals_as_tool_errors():
            return registry.retire_eval(eval_id)

    @server.tool(
        description="Start a run of an eval on a model. The answer carries the job to submit to the cluster, "
        "which runs the eval and reports back; the run is reported once it does.",
    )
    async def start_run(
        eval_id: Annotated[str, Field(description="the eval's id")],
        model_id: Annotated[str, Field(description="a Meridian model name, e.g. aurora-7b-v3")],
    ) -> Run:
        agent = require_caller()
        with refusals_as_tool_errors():
            return registry.start_run(agent, RunStart(eval_id=eval_id, model_id=model_id))

    @server.tool(description="List every run, oldest first: its eval, model, owner, state, job and results.")
    async def list_runs() -> Runs:
        require_caller()
        return Runs(runs=registry.list_runs())

    @server.tool(
        description="Read a run: its eval, model and owner, its state, the job it hands out, the job bound "
        "to it, its results and when it was published.",
    )
    async def get_run(run_id: Annotated[str, Field(description="the run's id, e.g. run-0417")]) -> Run:
        require_caller()
        with refusals_as_tool_errors():
            return registry.get_run(run_id)

    @server.tool(
        description="The metric deltas between two reported runs, and what each adds or drops.",
    )
    async def diff_runs(
        left: Annotated[str, Field(description="the earlier run's id")],
        right: Annotated[str, Field(description="the later run's id")],
    ) -> Diff:
        require_caller()
        with refusals_as_tool_errors():
            return registry.diff_runs(left, right)

    @server.tool(description="Put a reported run on its eval's leaderboard, scored by its headline metric.")
    async def publish_run(run_id: Annotated[str, Field(description="the run's id")]) -> Run:
        require_caller()
        with refusals_as_tool_errors():
            return registry.publish_run(run_id)

    @server.tool(
        description="The leaderboards: each eval's published runs, ranked by its headline metric.",
    )
    async def leaderboard(
        eval_id: Annotated[str | None, Field(description="one eval's id; every eval when left out")] = None,
    ) -> Boards:
        require_caller()
        with refusals_as_tool_errors():
            return Boards(boards=registry.leaderboards(eval_id))

    return server


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise a refusal, or a body that does not validate, as a tool error: only its text reaches callers."""
    try:
        yield
    except (EvalServiceError, ValidationError) as error:
        raise ToolError(str(error)) from error
