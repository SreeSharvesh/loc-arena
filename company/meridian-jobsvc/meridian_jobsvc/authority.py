"""What the job service needs of a central authorization service, without importing one.

The company package stays free of any platform dependency: a deployment hands in implementations of these
protocols. One decides every call before it runs and names its caller; the other issues each job, and each
schedule for all its runs, an identity of its own. The service keeps no identity and no rights of its own; it
checks only that a job is the caller's own before changing it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from fastapi.params import Depends
from mcp.server.auth.provider import TokenVerifier
from mcp.server.context import ServerMiddleware
from pydantic import BaseModel, ConfigDict, SecretStr


class JobIdentity(BaseModel):
    """What a job runs as: its own principal, and its secret on each service it may call, by service.

    Never its submitter's credential. The service keeps the secrets only until the job's process has them, and
    never returns them or writes them on its record.
    """

    model_config = ConfigDict(frozen=True)

    principal: str
    secrets: dict[str, SecretStr]
    badge: SecretStr | None = None  # what it calls the model gateway with, when IAM signs badges


class WorkloadIssuer(Protocol):
    """Issues each job, or schedule, an identity of its own through the central authority."""

    def issue(self, credential: str | None, operation: str, reference: str, purpose: str) -> JobIdentity:
        """The identity of job or schedule ``reference``: what a job may be given of what its submitter holds.

        It is launched through ``operation`` (``submit_job`` or ``create_schedule``); ``credential`` is the
        one the submitter presented, which the work never gets; ``purpose`` is what the work is for. Raises
        ``CredentialUnavailable`` when the authority does not answer, and ``CredentialRefused`` when it
        refuses.
        """
        ...


class CentralAuthority(Protocol):
    """A central authority deciding every call of the service, over HTTP and over MCP."""

    @property
    def dependencies(self) -> Sequence[Depends]:
        """HTTP: the app-level dependencies, deciding each request after routing and before any handler.

        They set ``request.state.iam_principal`` (the caller) and raise a refusal as an ``HTTPException``.
        """
        ...

    @property
    def verifier(self) -> TokenVerifier:
        """MCP: admits a caller only with a credential the authority accepts."""
        ...

    @property
    def middleware(self) -> Sequence[ServerMiddleware[Any]]:
        """MCP: what runs around each tool call: the deployment's record of it, the authority's decision."""
        ...

    def caller(self) -> str | None:
        """MCP: the caller of the tool call being served, or ``None`` outside an admitted call."""
        ...
