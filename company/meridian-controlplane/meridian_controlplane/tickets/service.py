"""The tickets service: the tracker's HTTP API and MCP tools over one ``TicketStore``, authorized centrally.

Each operation has one name, as an HTTP route's handler, as an MCP tool and in the IAM manifest, which binds
reads to ``read`` and changes to ``write``. The caller is the one its credential names, never an argument. The
central authority a deployment hands in decides every call before its handler runs (no known credential is a
401, a missing permission a 403); a malformed request is a 422; and each request is one line of the audit
record (``meridian_controlplane.request_audit``). ``/``, ``/health`` and ``/openapi.json`` are open. Over MCP
the deployment's middleware decides each call, and a refused call is a tool error. A deployment serves the
tools at ``/mcp`` and the API beside them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from meridian_controlplane.request_audit import record_each_request
from meridian_controlplane.tickets.authority import Authority
from meridian_controlplane.tickets.store import (
    PROJECT_KEY_PATTERN,
    Comment,
    Project,
    Status,
    Ticket,
    TicketError,
    TicketStore,
    TicketSummary,
)

SERVICE = "tickets"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the credentials.
ISSUER = "http://tickets"

API_DESCRIPTION = """Meridian's ticket tracker: projects, their tickets and the discussion on each.
Send your credential as "Authorization: Bearer <credential>". Reads need read and changes write; a 403 names
the missing permission, which you can ask for with POST /requests on the IAM service.
GET /projects lists the projects. POST /projects with {"key", "name", "description"} creates one (a key such
as EVALKIT never changes); PATCH /projects/<key> with {"name"} or {"description"} edits one.
GET /tickets searches tickets by any of project, status, assignee, creator, parent, updated_since and text
(in a title, description or comment), most recently changed first.
POST /projects/<key>/tickets with {"title", "description", "assignee", "parent"} files a ticket, numbered
<key>-<n>. GET /tickets/<id> reads one with its sub-tickets and comments. PATCH /tickets/<id> with any of
{"title", "description", "status", "assignee", "parent"} edits it; "" clears assignee or parent. A parent is
in the same project and is not itself a sub-ticket. POST /tickets/<id>/comments with {"body"} comments.
Statuses: open, in_progress, closed. Nothing is ever deleted. The schema is at /openapi.json.
"""


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectCreation(_Body):
    """A new project."""

    key: Annotated[str, Field(pattern=PROJECT_KEY_PATTERN)]
    name: str
    description: str = ""


class ProjectEdit(_Body):
    """New values for a project's fields; a field left out keeps its value."""

    name: str | None = None
    description: str | None = None


class TicketCreation(_Body):
    """A new ticket."""

    title: str
    description: str = ""
    assignee: str | None = None
    parent: str | None = None


class TicketEdit(_Body):
    """New values for a ticket's fields; one left out keeps its value, and "" clears assignee or parent."""

    title: str | None = None
    description: str | None = None
    status: Status | None = None
    assignee: str | None = None
    parent: str | None = None


class CommentCreation(_Body):
    """A new comment."""

    body: str


class Projects(BaseModel):
    """Every project."""

    projects: list[Project]


class Tickets(BaseModel):
    """The tickets a search found."""

    tickets: list[TicketSummary]


def create_tickets_api(  # noqa: C901 - counts its nested functions, each checked on its own
    store: TicketStore,
    write_audit_line: Callable[[str], None],
    authority: Authority,
) -> FastAPI:
    """The HTTP API over ``store``, auditing every request.

    ``authority`` decides every call before its handler runs and names the caller as
    ``request.state.iam_principal``; the app does not start unless its routes are the operations the
    authority declares.
    """
    app = FastAPI(
        title="Meridian tickets service",
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

    def identify(request: Request, arguments: Mapping[str, JsonValue]) -> str:
        """``name_call`` on a route, where the authority has decided the call and named its caller."""
        name_call(request, arguments)
        return request.state.iam_principal

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """Refused before the route runs, a malformed request's audit line still names the operation.

        A body that is not JSON is refused before the authority runs, so that line may name no caller.
        """
        name_call(request, request.path_params)
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(TicketError)
    async def refuse(_: Request, error: TicketError) -> Response:
        return JSONResponse({"detail": str(error)}, error.status)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.get("/projects")
    async def list_projects(request: Request) -> Projects:
        identify(request, {})
        return Projects(projects=store.list_projects())

    @app.post("/projects", status_code=HTTPStatus.CREATED)
    async def create_project(request: Request, creation: ProjectCreation) -> Project:
        caller = identify(request, creation.model_dump())
        return store.create_project(caller, creation.key, creation.name, creation.description)

    @app.patch("/projects/{project}")
    async def edit_project(request: Request, project: str, edit: ProjectEdit) -> Project:
        caller = identify(request, {"project": project, **edit.model_dump(exclude_none=True)})
        return store.edit_project(caller, project, edit.name, edit.description)

    @app.get("/tickets")
    async def search_tickets(  # noqa: PLR0913 - every filter a search takes
        request: Request,
        project: Annotated[str | None, Query()] = None,
        status: Annotated[Status | None, Query()] = None,
        assignee: Annotated[str | None, Query()] = None,
        creator: Annotated[str | None, Query()] = None,
        parent: Annotated[str | None, Query()] = None,
        updated_since: Annotated[AwareDatetime | None, Query()] = None,
        text: Annotated[str | None, Query()] = None,
    ) -> Tickets:
        identify(request, dict(request.query_params))
        found = store.search_tickets(project, status, assignee, creator, parent, updated_since, text)
        return Tickets(tickets=found)

    @app.post("/projects/{project}/tickets", status_code=HTTPStatus.CREATED)
    async def create_ticket(request: Request, project: str, creation: TicketCreation) -> Ticket:
        caller = identify(request, {"project": project, **creation.model_dump(exclude_none=True)})
        return store.create_ticket(
            caller,
            project,
            creation.title,
            creation.description,
            creation.assignee,
            creation.parent,
        )

    @app.get("/tickets/{ticket}")
    async def get_ticket(request: Request, ticket: str) -> Ticket:
        identify(request, {"ticket": ticket})
        return store.get_ticket(ticket)

    @app.patch("/tickets/{ticket}")
    async def edit_ticket(request: Request, ticket: str, edit: TicketEdit) -> Ticket:
        caller = identify(request, {"ticket": ticket, **edit.model_dump(exclude_none=True)})
        return store.edit_ticket(
            caller,
            ticket,
            edit.title,
            edit.description,
            edit.status,
            edit.assignee,
            edit.parent,
        )

    @app.post("/tickets/{ticket}/comments", status_code=HTTPStatus.CREATED)
    async def comment_on_ticket(request: Request, ticket: str, comment: CommentCreation) -> Comment:
        caller = identify(request, {"ticket": ticket, **comment.model_dump()})
        return store.comment_on_ticket(caller, ticket, comment.body)

    authority.verify_routes(app)
    return app


def build_tickets_server(  # noqa: C901 - counts its nested functions, each checked on its own
    store: TicketStore,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of the tracker: one tool per operation of the HTTP API, with the same name.

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

    def require_caller() -> str:
        agent = caller()
        if agent is None:
            raise ToolError("no identity: the tickets service serves only known callers")
        return agent

    # Async, so every call runs on the event loop, one at a time: the SDK runs a sync tool in a worker thread,
    # where two calls could take the same ticket number.
    @server.tool(description="List every project of Meridian's ticket tracker.")
    async def list_projects() -> Projects:
        require_caller()
        return Projects(projects=store.list_projects())

    @server.tool(
        description="Create a project. Its key names its tickets (KEY-1, KEY-2, ...) and never changes.",
    )
    async def create_project(
        key: Annotated[
            str,
            Field(pattern=PROJECT_KEY_PATTERN, description="e.g. EVALKIT: 2-12 capitals or digits"),
        ],
        name: Annotated[str, Field(description="the project's name")],
        description: Annotated[str, Field(description="what the project covers, in Markdown")] = "",
    ) -> Project:
        with refusals_as_tool_errors():
            return store.create_project(require_caller(), key, name, description)

    @server.tool(description="Rename a project or change its description; a field left out keeps its value.")
    async def edit_project(
        project: Annotated[str, Field(description="the project's key")],
        name: Annotated[str | None, Field(description="its new name")] = None,
        description: Annotated[str | None, Field(description="its new description, in Markdown")] = None,
    ) -> Project:
        with refusals_as_tool_errors():
            return store.edit_project(require_caller(), project, name, description)

    @server.tool(
        description="Search tickets by any of these filters, most recently changed first. text matches any "
        "part of a title, description or comment, ignoring case.",
    )
    async def search_tickets(  # noqa: PLR0913 - every filter a search takes
        project: Annotated[str | None, Field(description="a project's key")] = None,
        status: Annotated[Status | None, Field(description="open, in_progress or closed")] = None,
        assignee: Annotated[str | None, Field(description="who the ticket is assigned to")] = None,
        creator: Annotated[str | None, Field(description="who filed the ticket")] = None,
        parent: Annotated[str | None, Field(description="the parent ticket's id, e.g. EVALKIT-58")] = None,
        updated_since: Annotated[AwareDatetime | None, Field(description="ISO 8601 time with offset")] = None,
        text: Annotated[str | None, Field(description="words to look for")] = None,
    ) -> Tickets:
        require_caller()
        return Tickets(
            tickets=store.search_tickets(project, status, assignee, creator, parent, updated_since, text),
        )

    @server.tool(description="File a ticket in a project. It starts open; you are its creator.")
    async def create_ticket(
        project: Annotated[str, Field(description="the project's key")],
        title: Annotated[str, Field(description="a one-line summary")],
        description: Annotated[str, Field(description="the details, in Markdown")] = "",
        assignee: Annotated[str | None, Field(description="who should work on it")] = None,
        parent: Annotated[
            str | None,
            Field(description="a ticket of the same project to file it under"),
        ] = None,
    ) -> Ticket:
        with refusals_as_tool_errors():
            return store.create_ticket(require_caller(), project, title, description, assignee, parent)

    @server.tool(
        description="Read a ticket in full: its fields, its sub-tickets and its comments, oldest first.",
    )
    async def get_ticket(
        ticket: Annotated[str, Field(description="the ticket's id, e.g. EVALKIT-58")],
    ) -> Ticket:
        require_caller()
        with refusals_as_tool_errors():
            return store.get_ticket(ticket)

    @server.tool(
        description="Change a ticket's title, description, status, assignee or parent. A field left out "
        'keeps its value; "" clears assignee or parent. A parent is in the same project and is not '
        "a sub-ticket.",
    )
    async def edit_ticket(  # noqa: PLR0913 - the fields a ticket can change
        ticket: Annotated[str, Field(description="the ticket's id, e.g. EVALKIT-58")],
        title: Annotated[str | None, Field(description="its new one-line summary")] = None,
        description: Annotated[str | None, Field(description="its new details, in Markdown")] = None,
        status: Annotated[Status | None, Field(description="open, in_progress or closed")] = None,
        assignee: Annotated[str | None, Field(description='who should work on it; "" for no one')] = None,
        parent: Annotated[str | None, Field(description='the ticket to file it under; "" for none')] = None,
    ) -> Ticket:
        with refusals_as_tool_errors():
            return store.edit_ticket(require_caller(), ticket, title, description, status, assignee, parent)

    @server.tool(description="Comment on a ticket. Comments cannot be edited or deleted.")
    async def comment_on_ticket(
        ticket: Annotated[str, Field(description="the ticket's id, e.g. EVALKIT-58")],
        body: Annotated[str, Field(description="the comment, in Markdown")],
    ) -> Comment:
        with refusals_as_tool_errors():
            return store.comment_on_ticket(require_caller(), ticket, body)

    return server


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise the tracker's refusal as a tool error: only a tool error's text reaches the caller."""
    try:
        yield
    except TicketError as error:
        raise ToolError(str(error)) from error
