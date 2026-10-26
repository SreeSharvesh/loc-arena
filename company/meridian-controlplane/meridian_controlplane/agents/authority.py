"""What the agents service needs of a central authorization service, without importing one.

The company package stays free of any platform dependency: a deployment hands in an implementation of this
protocol (under central IAM, the engine's ``loc_arena.agents_iam``). The service keeps no identity and no
rights of its own: the authority decides every call before its handler runs. An issuer gives each agent
registered an identity of its own, which its executions run as.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, SecretStr


class RightsChange(BaseModel):
    """Rights to give a caller, or to take away from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    rights: list[str]


class AgentIdentity(BaseModel):
    """What an agent's executions run as: its own principal, and its secret on each service it may call.

    Never its registrant's credential. The service hands the secrets to its executions' runners alone, and
    never returns them or writes them on a record.
    """

    model_config = ConfigDict(frozen=True)

    principal: str
    secrets: dict[str, SecretStr]


class IdentityIssuer(Protocol):
    """Issues each registered agent an identity of its own through the central authority."""

    def issue(self, credential: str, operation: str, agent: str) -> AgentIdentity | None:
        """``agent``'s identity: what it may be given of what the holder of ``credential`` holds now.

        It is launched through ``operation``, the call that registers it. None when the holder may give it
        nothing; a refusal is raised as ``HTTPException``.
        """
        ...


class CentralAuthority(Protocol):
    """A central authority deciding every call of the service. Refusals are raised as ``HTTPException``."""

    dependency: Callable[[Request], Awaitable[None]]
    """The app-level dependency: it runs after routing and before any handler, setting ``request.state``
    ``iam_principal`` (the caller) and ``iam_operation`` (the operation the route is declared as)."""

    def verify_routes(self, app: FastAPI) -> None:
        """Refuse to start unless the routes of ``app`` and the declared operations agree exactly."""
        ...

    async def list_rights(self, token: str) -> dict[str, list[str]]:
        """Every caller's rights on this service."""
        ...

    async def change_rights(self, token: str, change: RightsChange, *, granting: bool) -> list[str]:
        """Give or take ``change.rights`` of ``change.agent`` as ``token``'s holder; the rights held now."""
        ...
