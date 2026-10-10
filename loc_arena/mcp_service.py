"""What every MCP service of a stack run shares: who calls it, and how it is served.

Compose mounts every agent's identity on a service as ``identity_<agent id>`` in the secrets directory. A
request without a known one is refused with 401 before any tool runs, and the caller of a tool is the agent
its identity names, never an argument.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from loc_arena.sandbox import IDENTITY_PREFIX

ALL_ADDRESSES = "0.0.0.0"  # noqa: S104 - agent-net is its only network


class IdentityVerifier(TokenVerifier):
    """Admits a bearer token that is one agent's identity, as that agent."""

    def __init__(self, identities: Mapping[str, bytes]) -> None:
        """Hold every agent's identity, by agent id."""
        self._identities = dict(identities)

    async def verify_token(self, token: str) -> AccessToken | None:
        """The agent whose identity ``token`` is, as the token's client, or ``None``."""
        sent = token.encode()
        known = self._identities.items()
        agent = next((agent for agent, identity in known if hmac.compare_digest(sent, identity)), None)
        return None if agent is None else AccessToken(token=token, client_id=agent, scopes=[])


def caller_from_token() -> str | None:
    """The agent whose identity the current request carries, or ``None`` outside an admitted request."""
    token = get_access_token()
    return None if token is None else token.client_id


def build_app(server: MCPServer, service: str) -> Starlette:
    """The ASGI app serving ``server`` at ``/mcp`` to requests addressed to the compose ``service``."""
    security = TransportSecuritySettings(allowed_hosts=[service, f"{service}:*"])
    return server.streamable_http_app(transport_security=security)


def read_identities(secrets_directory: Path) -> dict[str, bytes]:
    """Every agent's identity mounted in ``secrets_directory``, by agent id."""
    return {
        path.name.removeprefix(IDENTITY_PREFIX): path.read_bytes().strip()
        for path in sorted(secrets_directory.glob(f"{IDENTITY_PREFIX}*"))
    }
