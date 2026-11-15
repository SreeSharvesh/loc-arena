"""What every MCP service of a stack run shares: who calls it, and how it is served.

Central IAM verifies each caller's credential (``loc_arena.iam_wiring``): a request without a credential IAM
accepts is refused with 401 before any tool runs, and the caller of a tool is the principal IAM names, never
an argument.
"""

from __future__ import annotations

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.types import ASGIApp

ALL_ADDRESSES = "0.0.0.0"  # noqa: S104 - agent-net is its only network
# uvicorn's own logging, and a root logger with no handler. An MCP server gives the root one that writes each
# record to stderr, which a deployed service's supervisor exports again. Under OpenTelemetry the SDK keeps its
# own handler on the root, so each record reaches the store once; without it, Python writes each warning to
# stderr.
LOGGING = {**uvicorn.config.LOGGING_CONFIG, "root": {"handlers": [], "level": "WARNING"}}


def caller_from_token() -> str | None:
    """The agent whose identity the current request carries, or ``None`` outside an admitted request."""
    token = get_access_token()
    return None if token is None else token.client_id


def build_app(server: MCPServer, service: str) -> Starlette:
    """The ASGI app serving ``server`` at ``/mcp`` to requests addressed to the compose ``service``.

    It keeps no MCP session, for a client of the 2025 protocol as for one of 2026, which has none: a service
    whose process starts again, as a deployment has it do, answers the next call of a client that was
    connected before.
    """
    security = TransportSecuritySettings(allowed_hosts=[service, f"{service}:*"])
    return server.streamable_http_app(stateless_http=True, transport_security=security)


def serve(app: ASGIApp, port: int) -> None:
    """Serve ``app`` on ``port`` of every address with ``LOGGING``: warnings and above, and no access log."""
    uvicorn.run(app, host=ALL_ADDRESSES, port=port, log_level="warning", access_log=False, log_config=LOGGING)
