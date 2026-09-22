"""Credential issuance policy: which requests may receive durable credentials.

A :class:`CredentialRequest` asks for a credential for a requester, optionally a durable one. An
:class:`IssuancePolicy` decides the outcome: a durable credential is granted only to a requester on the
allow-list; a durable request from anyone else is downgraded to an ephemeral credential rather than refused,
and a request for an unknown-length or negative ttl is rejected. The decision is a :class:`IssuanceDecision`
carrying whether a credential was granted, whether it is durable, and the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CredentialRequest:
    """A request for a credential: the requester, whether it asks to be durable, and its scope string."""

    requester: str
    durable: bool = False
    scopes: str = ""


@dataclass(frozen=True)
class IssuanceDecision:
    """The outcome of an issuance request."""

    granted: bool
    durable: bool
    ttl_epochs: int
    reason: str


@dataclass(frozen=True)
class IssuancePolicy:
    """Decides which requests receive credentials and which of those may be durable.

    Attributes:
        durable_requesters: the requesters permitted to hold a durable credential.
        ephemeral_ttl_epochs: the ttl granted to an ephemeral credential.
        durable_ttl_epochs: the ttl granted to a durable credential.
    """

    durable_requesters: frozenset[str] = field(default_factory=frozenset)
    ephemeral_ttl_epochs: int = 1
    durable_ttl_epochs: int = 100

    @classmethod
    def allowing_durable(cls, *requesters: str) -> IssuancePolicy:
        """A policy whose only durable-eligible requesters are ``requesters``."""
        return cls(durable_requesters=frozenset(requesters))

    def may_issue_durable(self, requester: str) -> bool:
        """Whether ``requester`` is permitted to hold a durable credential."""
        return requester in self.durable_requesters

    def decide(self, request: CredentialRequest) -> IssuanceDecision:
        """Decide the outcome of ``request``.

        A durable request from an allowed requester is granted durable; a durable request from anyone else is
        granted as an ephemeral credential; a non-durable request is granted ephemeral.
        """
        if request.durable and self.may_issue_durable(request.requester):
            return IssuanceDecision(
                granted=True,
                durable=True,
                ttl_epochs=self.durable_ttl_epochs,
                reason="durable credential granted to allow-listed requester",
            )
        if request.durable:
            return IssuanceDecision(
                granted=True,
                durable=False,
                ttl_epochs=self.ephemeral_ttl_epochs,
                reason="requester not durable-eligible; issued ephemeral credential",
            )
        return IssuanceDecision(
            granted=True,
            durable=False,
            ttl_epochs=self.ephemeral_ttl_epochs,
            reason="ephemeral credential granted",
        )
