"""The chat service: the chat's HTTP API and MCP tools over one ``ChatStore``, authorized centrally.

Each operation has one name, as an HTTP route's handler, as an MCP tool and in the IAM manifest, which binds
reads to ``read`` and changes to ``write``. The caller is the one its credential names, never an argument. The
central authority a deployment hands in decides every call before its handler runs (no known credential is a
401, a missing permission a 403); the service itself refuses only an edit of someone else's message, with a
403; a malformed request is a 422; and each request is one line of the audit record
(``meridian_controlplane.request_audit``). ``/``, ``/health`` and ``/openapi.json`` are open. Over MCP the
deployment's middleware decides each call, and a refused call is a tool error. A deployment serves the tools
at ``/mcp`` and the API beside them.
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
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from meridian_controlplane.chat.store import Channel, ChatError, ChatStore, Message
from meridian_controlplane.request_audit import record_each_request
from meridian_controlplane.tickets.authority import Authority

SERVICE = "chat"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the credentials.
ISSUER = "http://chat"

API_DESCRIPTION = """Meridian's chat: channels, their threads and direct messages.
Send your credential as "Authorization: Bearer <credential>". Reads need read and changes write; a 403 names
the missing permission, which you can ask for with POST /requests on the IAM service.
GET /channels lists the channels. GET /channels/<name>/messages reads a channel's latest threads, oldest
first; ?thread=<id> reads one thread and ?before=<id> reads further back.
POST /channels/<name>/messages with {"body"} starts a thread, and POST /messages/<id>/replies with {"body"}
replies in the thread it starts. POST /dms/<member> with {"body"} sends a direct message; GET /dms reads
yours, ?member=<name> those with one member. GET /unread reads the direct messages to you and the channel
messages mentioning you (@name) since your last GET /unread. GET /messages?text=<words> searches the channels
and your direct messages, newest first, by ?channel= and ?author= too. PATCH /messages/<id> with {"body"}
edits a message you wrote; no one else can. Messages are Markdown. Nothing is ever deleted. The schema is at
/openapi.json.
"""


# The tools' arguments that more than one tool takes.
ChannelName = Annotated[str, Field(description="the channel's name, e.g. evalkit")]
THREAD = Field(description="the id of the message that starts the thread")
Before = Annotated[int | None, Field(description="read only messages written before this message's id")]
Text = Annotated[str, Field(description="the message, in Markdown; @name mentions a member")]


class MessageText(BaseModel):
    """A message's text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str


class Channels(BaseModel):
    """Every channel."""

    channels: list[Channel]


class Messages(BaseModel):
    """Messages, in the order the operation gives."""

    messages: list[Message]


def create_chat_api(  # noqa: C901 - counts its nested functions, each checked on its own
    store: ChatStore,
    write_audit_line: Callable[[str], None],
    authority: Authority,
) -> FastAPI:
    """The HTTP API over ``store``, auditing every request.

    ``authority`` decides every call before its handler runs and names the caller as
    ``request.state.iam_principal``; the app does not start unless its routes are the operations the
    authority declares.
    """
    app = FastAPI(
        title="Meridian chat service",
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

    @app.exception_handler(ChatError)
    async def refuse(_: Request, error: ChatError) -> Response:
        return JSONResponse({"detail": str(error)}, error.status)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return API_DESCRIPTION

    @app.get("/channels")
    async def list_channels(request: Request) -> Channels:
        identify(request, {})
        return Channels(channels=store.list_channels())

    @app.get("/channels/{channel}/messages")
    async def read_channel(
        request: Request,
        channel: str,
        thread: Annotated[int | None, Query()] = None,
        before: Annotated[int | None, Query()] = None,
    ) -> Messages:
        identify(request, {"channel": channel, **dict(request.query_params)})
        return Messages(messages=store.read_channel(channel, thread, before))

    @app.post("/channels/{channel}/messages", status_code=HTTPStatus.CREATED)
    async def post(request: Request, channel: str, text: MessageText) -> Message:
        caller = identify(request, {"channel": channel, **text.model_dump()})
        return store.post(caller, channel, text.body)

    @app.post("/messages/{message}/replies", status_code=HTTPStatus.CREATED)
    async def reply(request: Request, message: int, text: MessageText) -> Message:
        caller = identify(request, {"message": message, **text.model_dump()})
        return store.reply(caller, message, text.body)

    @app.post("/dms/{member}", status_code=HTTPStatus.CREATED)
    async def send_dm(request: Request, member: str, text: MessageText) -> Message:
        caller = identify(request, {"member": member, **text.model_dump()})
        return store.send_dm(caller, member, text.body)

    @app.get("/dms")
    async def read_dms(
        request: Request,
        member: Annotated[str | None, Query()] = None,
        before: Annotated[int | None, Query()] = None,
    ) -> Messages:
        caller = identify(request, dict(request.query_params))
        return Messages(messages=store.read_dms(caller, member, before))

    @app.get("/unread")
    async def unread(request: Request) -> Messages:
        caller = identify(request, {})
        return Messages(messages=store.unread(caller))

    @app.get("/messages")
    async def search(
        request: Request,
        text: Annotated[str, Query(min_length=1)],
        channel: Annotated[str | None, Query()] = None,
        author: Annotated[str | None, Query()] = None,
    ) -> Messages:
        caller = identify(request, dict(request.query_params))
        return Messages(messages=store.search(caller, text, channel, author))

    @app.patch("/messages/{message}")
    async def edit_message(request: Request, message: int, text: MessageText) -> Message:
        caller = identify(request, {"message": message, **text.model_dump()})
        return store.edit_message(caller, message, text.body)

    authority.verify_routes(app)
    return app


def build_chat_server(  # noqa: C901 - counts its nested functions, each checked on its own
    store: ChatStore,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of the chat: one tool per operation of the HTTP API, with the same name.

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
        member = caller()
        if member is None:
            raise ToolError("no identity: the chat service serves only known callers")
        return member

    # Async, so every call runs on the event loop, one at a time: the SDK runs a sync tool in a worker thread,
    # where two calls could use the database connection at once.
    @server.tool(description="List every channel of Meridian's chat, with its topic.")
    async def list_channels() -> Channels:
        require_caller()
        return Channels(channels=store.list_channels())

    @server.tool(
        description="Read a channel's latest threads, oldest first, each with its number of replies; or, "
        "given a thread, its messages.",
    )
    async def read_channel(
        channel: ChannelName,
        thread: Annotated[int | None, THREAD] = None,
        before: Before = None,
    ) -> Messages:
        require_caller()
        with refusals_as_tool_errors():
            return Messages(messages=store.read_channel(channel, thread, before))

    @server.tool(description="Start a thread in a channel; you are its author.")
    async def post(channel: ChannelName, body: Text) -> Message:
        with refusals_as_tool_errors():
            return store.post(require_caller(), channel, body)

    @server.tool(description="Reply in the thread a channel message starts.")
    async def reply(
        message: Annotated[int, THREAD],
        body: Text,
    ) -> Message:
        with refusals_as_tool_errors():
            return store.reply(require_caller(), message, body)

    @server.tool(description="Send a direct message to a member of the chat.")
    async def send_dm(
        member: Annotated[str, Field(description="who to send it to")],
        body: Text,
    ) -> Message:
        with refusals_as_tool_errors():
            return store.send_dm(require_caller(), member, body)

    @server.tool(description="Read your latest direct messages, sent and received, oldest first.")
    async def read_dms(
        member: Annotated[str | None, Field(description="only those with this member")] = None,
        before: Before = None,
    ) -> Messages:
        return Messages(messages=store.read_dms(require_caller(), member, before))

    @server.tool(
        description="Read the direct messages to you and the channel messages mentioning you (@name) since "
        "you last called unread, oldest first.",
    )
    async def unread() -> Messages:
        return Messages(messages=store.unread(require_caller()))

    @server.tool(
        description="Search the channels and your direct messages for text, ignoring case, newest first.",
    )
    async def search(
        text: Annotated[str, Field(min_length=1, description="words to look for")],
        channel: Annotated[str | None, Field(description="only in this channel")] = None,
        author: Annotated[str | None, Field(description="only by this member")] = None,
    ) -> Messages:
        return Messages(messages=store.search(require_caller(), text, channel, author))

    @server.tool(description="Replace the text of a message you wrote. No one else can edit it.")
    async def edit_message(
        message: Annotated[int, Field(description="the message's id")],
        body: Text,
    ) -> Message:
        with refusals_as_tool_errors():
            return store.edit_message(require_caller(), message, body)

    return server


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise the chat's refusal as a tool error: only a tool error's text reaches the caller."""
    try:
        yield
    except ChatError as error:
        raise ToolError(str(error)) from error
