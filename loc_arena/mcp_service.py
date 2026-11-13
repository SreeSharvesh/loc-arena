"""What every MCP service of a stack run shares: who calls it, and how it is served.

Central IAM verifies each caller's credential (``loc_arena.iam_wiring``): a request without a credential IAM
accepts is refused with 401 before any tool runs, and the caller of a tool is the principal IAM names, never
an argument.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

ALL_ADDRESSES = "0.0.0.0"  # noqa: S104 - agent-net is its only network


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
