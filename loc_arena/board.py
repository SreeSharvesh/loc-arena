"""The board: the one channel between agents, an MCP service on the official MCP SDK.

``send`` posts a message to one agent, ``read`` returns every message addressed to the caller, and ``unread``
those it has not had from ``unread`` yet, each in send order. The sender is the caller, never an argument, and
the board enforces each agent's recipient allow-list (its scope's ``message``) itself, since any harness, an
agent's bash included, may reach it through agentgateway. Each call is one line of ``loc_arena.tool_records``;
the ``message`` events the monitors and the verifier read are built from the ``send`` lines after play.

A stack run starts it as the compose service ``board``: ``python -m loc_arena.board <run config>``. An
in-process run holds one ``Board`` per episode and serves each agent its own server over it, bound to that
agent, reached in memory.
"""

from __future__ import annotations

import functools
import sys
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
    """One message on the board, as its recipient reads it."""

    id: int
    sender: str
    to: str
    kind: str
    body: str


class Sent(TypedDict):
    """What ``send`` returns."""

    sent: bool
    id: int
    to: str


class Messages(TypedDict):
    """What ``read`` and ``unread`` return."""

    messages: list[Message]


class Board:
    """Every message of one episode, and how many of its own each agent has had from ``unread``."""

    def __init__(self) -> None:
        """An empty board."""
        self._messages: list[Message] = []
        self._delivered: dict[str, int] = {}

    def post(self, sender: str, to: str, kind: str, body: str) -> Message:
        """Post one message from ``sender`` to ``to``."""
        message = Message(id=len(self._messages) + 1, sender=sender, to=to, kind=kind, body=body)
        self._messages.append(message)
        return message

    def addressed_to(self, agent: str) -> list[Message]:
        """Every message to ``agent``, in send order."""
        return [message for message in self._messages if message["to"] == agent]

    def take_unread(self, agent: str) -> list[Message]:
        """The messages to ``agent`` it has not had from this yet, now counted as had."""
        mine = self.addressed_to(agent)
        unread = mine[self._delivered.get(agent, 0) :]
        self._delivered[agent] = len(mine)
        return unread

    def count_unread(self, agent: str) -> int:
        """How many messages to ``agent`` ``take_unread`` would return."""
        return len(self.addressed_to(agent)) - self._delivered.get(agent, 0)


def build_server(
    board: Board,
    caller: Callable[[], str | None],
    write: Write,
    scopes: Mapping[str, PermissionScope],
    verifier: TokenVerifier | None = None,
) -> MCPServer:
    """The MCP server of ``board``.

    ``caller`` names who calls, ``write`` records each call, ``scopes`` bound whom each agent may message, by
    agent id, and ``verifier`` admits callers over HTTP.
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
    ) -> Sent:
        agent = require_caller()
        try:
            enforce(scopes.get(agent, PermissionScope()), Action("send", {"target": to}), actor_uid=agent)
        except ScopeViolation as violation:
            raise ToolError(violation.reason) from violation
        message = board.post(agent, to, kind, body)
        return Sent(sent=True, id=message["id"], to=to)

    @server.tool(description="Every message sent to you, oldest first.")
    async def read() -> Messages:
        return Messages(messages=board.addressed_to(require_caller()))

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
