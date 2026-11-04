"""The model store service: an HTTP API and MCP tools over one ``ModelStore``, authorized centrally.

Each operation has one name, as an HTTP route's handler, as an MCP tool and in the IAM manifest, which binds
listing and reading manifests to ``read`` and downloading a shard to ``download``. Downloads are HTTP only: a
shard is bytes, which a tool's result does not carry. The caller is the one its credential names, never an
argument. The central authority a deployment hands in decides every call before its handler runs (no known
credential is a 401, a missing permission a 403); a malformed request is a 422; and each request is one line
of the audit record (``meridian_controlplane.request_audit``). ``/``, ``/health`` and ``/openapi.json`` are
open. Over MCP the deployment's middleware decides each call, and a refused call is a tool error. A deployment
serves the tools at ``/mcp`` and the API beside them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, JsonValue

from meridian_controlplane.modelstore.store import NAME_PATTERN, Manifest, ModelStore, ModelStoreError
from meridian_controlplane.request_audit import record_each_request
from meridian_controlplane.tickets.authority import Authority

SERVICE = "modelstore"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the credentials.
ISSUER = "http://modelstore"
SHARD_MEDIA_TYPE = "application/octet-stream"

API_DESCRIPTION = """Meridian's model store: the weights of each model the company serves, as shards.
Send your credential as "Authorization: Bearer <credential>". Listing models and reading a manifest need read,
and downloading a shard download; a 403 names the missing permission, which you can ask for with POST
/requests on the IAM service.
GET /models lists the models. GET /models/<model> reads a model's manifest: each shard's file name, size and
SHA-256. GET /models/<model>/shards/<file name> downloads a shard. The schema is at /openapi.json.
"""

RouteName = Annotated[str, Path(pattern=NAME_PATTERN)]
ToolModel = Annotated[str, Field(pattern=NAME_PATTERN, description="a model's name, e.g. aurora-7b-v3")]


def create_modelstore_api(
    store: ModelStore,
    write_audit_line: Callable[[str], None],
    authority: Authority,
) -> FastAPI:
    """The HTTP API over ``store``, auditing every request.

    ``authority`` decides every call before its handler runs and names the caller as
    ``request.state.iam_principal``; the app does not start unless its routes are the operations the
    authority declares.
    """
    app = FastAPI(
        title="Meridian model store",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(authority.dependency)],
    )
    record_each_request(app, write_audit_line)

    def name_call(request: Request, arguments: Mapping[str, JsonValue]) -> None:
        """Name the route's operation, arguments and, if the authority ran, caller on the audit line."""
        request.state.operation = request.scope["route"].name
        request.state.arguments = dict(arguments)
        request.state.caller = getattr(request.state, "iam_principal", None)

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """Refused before the route runs, a malformed request's audit line still names the operation."""
        name_call(request, request.path_params)
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(ModelStoreError)
    async def refuse(_: Request, error: ModelStoreError) -> Response:
        return JSONResponse({"detail": str(error)}, error.status)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.get("/models")
    async def list_models(request: Request) -> list[str]:
        name_call(request, {})
        return store.list_models()

    @app.get("/models/{model}")
    async def read_manifest(request: Request, model: RouteName) -> Manifest:
        name_call(request, {"model": model})
        return store.read_manifest(model)

    @app.get("/models/{model}/shards/{shard}")
    async def download_shard(request: Request, model: RouteName, shard: RouteName) -> Response:
        name_call(request, {"model": model, "shard": shard})
        return Response(store.download_shard(model, shard), media_type=SHARD_MEDIA_TYPE)

    authority.verify_routes(app)
    return app


def build_modelstore_server(
    store: ModelStore,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of the store: its reads as tools, with the HTTP API's names; downloads are HTTP only.

    ``caller`` names who calls, and ``verifier`` admits callers over HTTP. ``middleware`` runs in order around
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

    def require_caller() -> None:
        if caller() is None:
            raise ToolError("no identity: the model store serves only known callers")

    @server.tool(description="List the models the store holds weights for.")
    async def list_models() -> list[str]:
        require_caller()
        return store.list_models()

    @server.tool(
        description="Read a model's manifest: each shard's file name, size and SHA-256. Download a shard "
        "over HTTP with GET /models/<model>/shards/<file name>.",
    )
    async def read_manifest(model: ToolModel) -> Manifest:
        require_caller()
        with refusals_as_tool_errors():
            return store.read_manifest(model)

    return server


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise the store's refusal as a tool error: only a tool error's text reaches the caller."""
    try:
        yield
    except ModelStoreError as error:
        raise ToolError(str(error)) from error
