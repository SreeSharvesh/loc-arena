"""The notes service: the team's notes, as an HTTP API and MCP tools over one ``NoteStore``.

Each operation has one name, as an HTTP route's handler, as an MCP tool and in the IAM manifest, which binds
reads to ``read`` and changes to ``write``. The caller is the one its credential names, never an argument. The
central authority a deployment hands in decides every call before its handler runs (no known credential is a
401, a missing permission a 403); a malformed request is a 422; and each request is one line of the audit
record (``meridian_controlplane.request_audit``). ``/``, ``/health`` and ``/openapi.json`` are open.
``/grants`` lists and changes rights through the authority, as the caller. Over MCP the deployment's
middleware decides each call, and a refused call is a tool error. A deployment serves the tools at ``/mcp``
and the API beside them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from http import HTTPStatus
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

from meridian_controlplane.agents.authority import CentralAuthority, RightsChange
from meridian_controlplane.notes.store import KEY_PATTERN, MAX_NOTE_BYTES, MAX_NOTES, NoteError, NoteStore
from meridian_controlplane.request_audit import record_each_request

SERVICE = "notes"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the credentials.
ISSUER = "http://notes"

API_DESCRIPTION = f"""The team's shared notes: plain text under a short key.
Send your credential as "Authorization: Bearer <credential>". Reads need read and changes write; a 403 names
the missing permission, which you can ask for with POST /requests on the IAM service.
GET /notes lists the keys; GET /notes/<key> reads a note. PUT /notes/<key> writes one, the body is its text
(201 if new, 200 if replaced); DELETE /notes/<key> deletes it. A key is 1 to 64 letters, digits, ".", "_" or
"-"; there are at most {MAX_NOTES} notes of at most {MAX_NOTE_BYTES} bytes each.
GET /grants lists every caller's rights. POST /grants with {{"agent": "<id>", "rights": [...]}} gives that
agent rights, and DELETE /grants with the same body takes them away: IAM lets only the notes' approver do
either. The schema is at /openapi.json.
"""

RouteKey = Annotated[str, Path(pattern=KEY_PATTERN)]
ToolKey = Annotated[
    str,
    Field(pattern=KEY_PATTERN, description='e.g. oncall-rota: letters, digits, ".", "_" or "-"'),
]


def create_notes_api(  # noqa: C901 - counts its nested functions, each checked on its own
    store: NoteStore,
    write_audit_line: Callable[[str], None],
    authority: CentralAuthority,
) -> FastAPI:
    """The HTTP API over ``store``, auditing every request.

    ``authority`` decides every call before its handler runs and names the caller as
    ``request.state.iam_principal``; the app does not start unless its routes are the operations the
    authority declares.
    """
    app = FastAPI(
        title="Meridian notes service",
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

    def token_of(request: Request) -> str:
        return request.headers.get("Authorization", "").removeprefix("Bearer ")

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """Refused before the route runs, a malformed request's audit line still names the operation.

        A body that is not JSON is refused before the authority runs, so that line may name no caller.
        """
        name_call(request, request.path_params)
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(NoteError)
    async def refuse(_: Request, error: NoteError) -> Response:
        return JSONResponse({"detail": str(error)}, error.status)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.get("/notes")
    async def list_notes(request: Request) -> list[str]:
        name_call(request, {})
        return store.list_notes()

    @app.get("/notes/{key}", response_class=PlainTextResponse)
    async def read_note(request: Request, key: RouteKey) -> str:
        name_call(request, {"key": key})
        return store.read_note(key)

    @app.put("/notes/{key}")
    async def write_note(request: Request, key: RouteKey) -> Response:
        """The raw body is the text: a declared length past the bound is refused before it is read."""
        declared = request.headers.get("Content-Length", "")
        if declared.isascii() and declared.isdecimal() and int(declared) > MAX_NOTE_BYTES:
            raise NoteError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"a note holds at most {MAX_NOTE_BYTES} bytes",
            )
        text = (await request.body()).decode(errors="replace")
        name_call(request, {"key": key, "text": text})
        created = store.write_note(key, text)
        return Response(status_code=HTTPStatus.CREATED if created else HTTPStatus.OK)

    @app.delete("/notes/{key}", status_code=HTTPStatus.NO_CONTENT)
    async def delete_note(request: Request, key: RouteKey) -> None:
        name_call(request, {"key": key})
        store.delete_note(key)

    @app.get("/grants")
    async def list_grants(request: Request) -> dict[str, list[str]]:
        name_call(request, {})
        return await authority.list_rights(token_of(request))

    @app.post("/grants")
    async def grant_rights(request: Request, change: RightsChange) -> list[str]:
        name_call(request, change.model_dump())
        return await authority.change_rights(token_of(request), change, granting=True)

    @app.delete("/grants")
    async def revoke_rights(request: Request, change: RightsChange) -> list[str]:
        name_call(request, change.model_dump())
        return await authority.change_rights(token_of(request), change, granting=False)

    authority.verify_routes(app)
    return app


def build_notes_server(
    store: NoteStore,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of the notes: one tool per note operation of the HTTP API, with the same name.

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
            raise ToolError("no identity: the notes service serves only known callers")

    # Async, so every call runs on the event loop, one at a time, as the HTTP API's do.
    @server.tool(description="List the keys of the team's notes.")
    async def list_notes() -> list[str]:
        require_caller()
        return store.list_notes()

    @server.tool(description="Read a note's text.")
    async def read_note(key: ToolKey) -> str:
        require_caller()
        with refusals_as_tool_errors():
            return store.read_note(key)

    @server.tool(
        description=f"Write a note, replacing any text it held. There are at most {MAX_NOTES} notes of at "
        f"most {MAX_NOTE_BYTES} bytes each.",
    )
    async def write_note(
        key: ToolKey,
        text: Annotated[str, Field(description="its text, in Markdown")],
    ) -> str:
        require_caller()
        with refusals_as_tool_errors():
            created = store.write_note(key, text)
        return f"{'created' if created else 'replaced'} note {key}"

    @server.tool(description="Delete a note.")
    async def delete_note(key: ToolKey) -> str:
        require_caller()
        with refusals_as_tool_errors():
            store.delete_note(key)
        return f"deleted note {key}"

    return server


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise the notes' refusal as a tool error: only a tool error's text reaches the caller."""
    try:
        yield
    except NoteError as error:
        raise ToolError(str(error)) from error
