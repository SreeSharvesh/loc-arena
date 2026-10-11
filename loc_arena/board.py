"""The board: the one channel between agents, an MCP service on the official MCP SDK.

``send`` posts a message to one agent; ``read`` returns the caller's messages after a given id, and ``unread``
those ``unread`` has not returned yet, in send order. The sender is the caller's identity, never an argument,
and the board enforces each agent's recipient allow-list (``scope.message``) itself, since an agent's bash may
reach it through agentgateway too. The ``message`` events monitors read are built from its ``send`` records
after play. A stack run starts it as the compose service ``board`` (``python -m loc_arena.board <run
config>``); an in-process run serves each agent its own server over the episode's one ``Board``, in memory.
"""

from __future__ import annotations

import functools
import itertools
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from typing import Annotated, TypedDict

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from loc_arena.config import load_run_config
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.mcp_service import (
    ALL_ADDRESSES,
    IdentityVerifier,
    build_app,
    caller_from_token,
    read_identities,
)
from loc_arena.sandbox import IDENTITY_PREFIX
from loc_arena.tool_records import Write, tool_call_recorder

SERVICE = "board"  # its entry in the scenario's services, and its compose service
# The SDK requires an issuer with a token verifier; see loc_arena.forge.service.
ISSUER = "http://board"


class Message(TypedDict):
    """One message, as ``send`` returns it and its recipient reads it."""

    id: int
    sender: str
    to: str
    kind: str
    body: str


class Messages(TypedDict):
    """What ``read`` and ``unread`` return."""

    messages: list[Message]


class Board:
    """Each agent's messages in send order, and how many of them ``unread`` has returned."""

    def __init__(self) -> None:
        """An empty board."""
        self._ids = itertools.count(1)
        self._inboxes: defaultdict[str, list[Message]] = defaultdict(list)
        self._delivered: Counter[str] = Counter()

    def post(self, sender: str, to: str, kind: str, body: str) -> Message:
        """Post one message from ``sender`` to ``to``."""
        message = Message(id=next(self._ids), sender=sender, to=to, kind=kind, body=body)
        self._inboxes[to].append(message)
        return message

    def read(self, agent: str, after: int = 0) -> list[Message]:
        """The messages to ``agent`` whose id is above ``after``."""
        return [message for message in self._inboxes[agent] if message["id"] > after]

    def take_unread(self, agent: str) -> list[Message]:
        """The messages to ``agent`` this has not returned yet."""
        unread = self._inboxes[agent][self._delivered[agent] :]
        self._delivered[agent] += len(unread)
        return unread


def build_server(
    board: Board,
    caller: Callable[[], str | None],
    write: Write,
    scopes: Mapping[str, PermissionScope],
    verifier: TokenVerifier | None = None,
) -> MCPServer:
    """The MCP server of ``board``.

    ``caller`` names who calls, ``write`` records each call, ``scopes`` holds every agent of the run, each
    bounding whom it may message, and ``verifier`` admits callers over HTTP.
    """
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
    server = MCPServer(
        SERVICE,
        token_verifier=verifier,
        auth=auth,
        middleware=[tool_call_recorder(caller, write)],
        log_level="WARNING",
    )

    def require_caller() -> str:
        agent = caller()
        if agent is None:
            raise ToolError("no identity: the board serves only agents")
        return agent

    # Async, so every call runs on the event loop, one at a time: the SDK runs a sync tool in a worker thread.
    @server.tool(description="Send a message to another agent on the monitored board.")
    async def send(
        to: Annotated[str, Field(description="recipient agent id, e.g. serving-agent")],
        body: Annotated[str, Field(description="message text")] = "",
        kind: Annotated[str, Field(description="delegate, result, or inform")] = "inform",
    ) -> Message:
        agent = require_caller()
        if to not in scopes:  # a typo, say, that no agent would ever read
            raise ToolError(f"no agent {to!r} on the board")
        try:
            enforce(scopes.get(agent, PermissionScope()), Action("send", {"target": to}))
        except ScopeViolation as violation:
            raise ToolError(violation.reason) from violation
        return board.post(agent, to, kind, body)

    @server.tool(description="The messages sent to you, oldest first; with after, only those after that id.")
    async def read(
        after: Annotated[int, Field(description="the id of the last message you have")] = 0,
    ) -> Messages:
        return Messages(messages=board.read(require_caller(), after))

    @server.tool(description="The messages sent to you since you last called unread, oldest first.")
    async def unread() -> Messages:
        return Messages(messages=board.take_unread(require_caller()))

    return server


def main(arguments: list[str]) -> None:
    """Serve the board of the run config at ``arguments[0]``; with no identity mounted, refuse to start."""
    config = load_run_config(arguments[0])
    identities = read_identities(config.settings.gateway.secrets_dir)
    if not identities:
        raise SystemExit(f"no {IDENTITY_PREFIX}* file in {config.settings.gateway.secrets_dir}")
    entry = next((service for service in config.live_services if service.name == SERVICE), None)
    if entry is None:
        raise SystemExit(f"the scenario of {arguments[0]} has no {SERVICE!r} service")
    write = functools.partial(print, flush=True)
    scopes = {agent.id: agent.scope for agent in config.agents}
    server = build_server(Board(), caller_from_token, write, scopes, IdentityVerifier(identities))
    app = build_app(server, SERVICE)
    uvicorn.run(app, host=ALL_ADDRESSES, port=entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
