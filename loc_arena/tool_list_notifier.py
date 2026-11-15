"""The tool-list notifier: a target of the tools gateway with no tools, which tells sessions to list again.

The gateway lists a session the tools of the targets that answer, and pi keeps a session's list until it is
told the list changed. The gateway sends no such notice of its own, so this target, which every session holds
and which keeps sessions, sends ``notifications/tools/list_changed`` on each session's stream when told to.
Only the gateway holds its key, and it writes no record.
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.session import ServerSession
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from loc_arena.tool_records import SESSION_HEADER
from loc_arena.workspace_service import KeyVerifier

TOOL_LIST_NOTIFIER = "tool-list-notifier"  # its target at the tools gateway, and the path it is served under
NOTIFIER_KEY_SECRET_NAME = "tool_list_notifier_key"  # the key the gateway calls it with: a compose secret
NOTIFIER_KEY_VARIABLE = "LOC_ARENA_TOOL_LIST_NOTIFIER_KEY"  # where compose reads that key from
_LOGGER = logging.getLogger(__name__)


class ToolListNotifier:
    """Each gateway session that opened here, and the notice that has each list its tools again."""

    def __init__(self, key: str, host: str) -> None:
        """A notifier that admits ``key`` alone, in requests addressed to ``host``."""
        self._sessions: dict[str, tuple[ServerSession, asyncio.AbstractEventLoop]] = {}
        server = MCPServer(
            TOOL_LIST_NOTIFIER,
            token_verifier=KeyVerifier({key: TOOL_LIST_NOTIFIER}),
            auth=AuthSettings(issuer_url=f"http://{host}", resource_server_url=None),
            middleware=[self._keep],
            log_level="WARNING",
        )
        security = TransportSecuritySettings(allowed_hosts=[host, f"{host}:*"])
        # It keeps sessions: a notice reaches a client only on its session's stream.
        self.app: Starlette = server.streamable_http_app(transport_security=security)

    async def _keep(self, ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.request is not None and (session := ctx.request.headers.get(SESSION_HEADER)):
            self._sessions[session] = (ctx.session, asyncio.get_running_loop())
        return await call_next(ctx)

    def notify(self, seconds: float) -> None:
        """Tell every session to list its tools again, waiting ``seconds`` at most for each to take it."""
        for session, (connection, loop) in list(self._sessions.items()):
            try:
                asyncio.run_coroutine_threadsafe(connection.send_tool_list_changed(), loop).result(seconds)
            except Exception as error:  # noqa: BLE001 - one session that takes no notice leaves the rest theirs
                _LOGGER.warning("session %s took no tool-list notice: %s", session, error)
