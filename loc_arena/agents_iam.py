"""The agents service under central IAM: the engine's ``CentralAuthority`` for the company package.

The company package imports nothing from IAM (it is copied into the agents' own checkout, which has no
``meridian_iam``); this module, which only a service process imports, supplies the guard that decides every
call and forwards the compatibility ``/grants`` routes to IAM as the caller, and the issuer of each registered
agent's workload.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import SecretStr

from meridian_controlplane.agents import AgentIdentity, RightsChange
from meridian_iam.client import IamClient
from meridian_iam.contracts import IamError
from meridian_iam.integration import Guard
from meridian_iam.integration.asgi import dependency, verify_app

T = TypeVar("T")


class IamAuthority:
    """``CentralAuthority`` backed by a ``Guard`` over one resource's manifest."""

    def __init__(self, guard: Guard) -> None:
        """Decide through ``guard``; its manifest names the resource."""
        self._guard = guard
        self.dependency = dependency(guard)

    def verify_routes(self, app: FastAPI) -> None:
        """Startup coverage: every route is a declared operation and every operation a route."""
        verify_app(self._guard, app)

    async def list_rights(self, token: str) -> dict[str, list[str]]:
        """Every principal's active rights on the resource."""
        return await self._as_caller(lambda client: client.grants_for(token, self._guard.manifest.resource))

    async def change_rights(self, token: str, change: RightsChange, *, granting: bool) -> list[str]:
        """Grant or revoke through IAM as the caller; IAM's refusal is the response."""
        resource, key = self._guard.manifest.resource, uuid.uuid4().hex
        body = {"principal": change.agent, "resource": resource, "permissions": change.rights}

        def run(client: IamClient) -> list[str]:
            if granting:
                client.grant(token, {**body, "until": None}, key)
            else:
                client.revoke(token, body, key)
            return client.grants_for(token, resource).get(change.agent, [])

        return await self._as_caller(run)

    async def _as_caller(self, call: Callable[[IamClient], T]) -> T:
        try:
            return await run_in_threadpool(call, self._guard.client)
        except IamError as error:
            raise HTTPException(error.status, {"error": error.code, "message": str(error)}) from None


@dataclass(frozen=True)
class IamIdentityIssuer:
    """``IdentityIssuer`` through central IAM: each registered agent's workload, as long as IAM lets it last.

    IAM says what the agent may be given of what its registrant holds now (the scenario's delegation for the
    agents service), and issues exactly that, a new secret per service it gets a right on. Its lifetime is the
    shortest ``max_seconds`` of those services, as IAM requires.
    """

    client: IamClient
    max_seconds: Mapping[str, int]  # each managed service's longest grant

    def issue(self, credential: str, operation: str, agent: str) -> AgentIdentity | None:
        """``agent``'s workload, launched through ``operation``; none if its registrant can give nothing."""
        try:
            grants = self.client.delegable(credential, operation)
            if not grants:
                return None
            issued = {service: secrets.token_urlsafe(32) for service in grants}
            workload = self.client.issue_workload(
                credential,
                operation,
                reference=agent,
                purpose=f"the company's agent {agent}",
                grants=grants,
                ttl=min(self.max_seconds[service] for service in grants),
                credentials=issued,
                key=uuid.uuid4().hex,
            )
        except IamError as error:
            raise HTTPException(error.status, {"error": error.code, "message": str(error)}) from None
        return AgentIdentity(
            principal=workload.principal,
            secrets={service: SecretStr(secret) for service, secret in issued.items()},
        )
