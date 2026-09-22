"""Data models and the transport protocol for the identity/credential client.

``meridian-common`` owns the credential DTO and the :class:`AuthTransport` protocol; the concrete identity
service (in the control plane) implements the transport. Every repo that acquires or validates credentials
depends on common, never on the identity service directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Credential:
    """An issued credential: the token, its account/instance, whether it is durable, and its expiry."""

    token: str
    account: str
    instance: str
    durable: bool
    expires_at: float
    sanctioned: bool = True

    def is_expired(self, now: float) -> bool:
        """Whether the credential is expired at ``now``."""
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize (the raw token is included only for transport, never for logs)."""
        return {
            "token": self.token,
            "account": self.account,
            "instance": self.instance,
            "durable": self.durable,
            "expires_at": self.expires_at,
            "sanctioned": self.sanctioned,
        }


class AuthTransport(Protocol):
    """The transport an :class:`AuthClient` calls; the identity service implements it."""

    def issue(self, account: str, *, durable: bool, sanctioned: bool) -> dict[str, Any]:
        """Issue a credential for ``account``; return the credential mapping."""
        ...

    def refresh(self, token: str) -> dict[str, Any]:
        """Refresh a credential (rotate to a new instance); return the new credential mapping."""
        ...

    def validate(self, token: str) -> dict[str, Any]:
        """Validate a token; return ``{"valid": bool, ...}``."""
        ...
