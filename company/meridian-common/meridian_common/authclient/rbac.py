"""Client-side RBAC scope parsing and checks.

Credentials carry scopes like ``repo:read`` or ``jobsvc:*``. :func:`parse_scopes` normalizes a scope string
into a :class:`ScopeSet`, and :meth:`ScopeSet.allows` checks whether a required ``resource:action`` is
granted,
honoring wildcards. This lets a caller fail fast on an insufficient credential before making a doomed request.
The authoritative RBAC decision still lives in the identity service; this is a fast local pre-check.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common.errors import ValidationError


@dataclass(frozen=True)
class Scope:
    """One granted scope: a ``resource`` and an ``action`` (either may be the wildcard ``*``)."""

    resource: str
    action: str

    def allows(self, resource: str, action: str) -> bool:
        """Whether this scope grants ``resource:action`` (wildcards match anything)."""
        res_ok = self.resource == "*" or self.resource == resource
        act_ok = self.action == "*" or self.action == action
        return res_ok and act_ok

    def __str__(self) -> str:
        """Render as ``resource:action``."""
        return f"{self.resource}:{self.action}"


class ScopeSet:
    """A set of granted :class:`Scope` objects with a membership check."""

    def __init__(self, scopes: list[Scope]) -> None:
        """Hold the granted scopes."""
        self._scopes = list(scopes)

    def allows(self, resource: str, action: str) -> bool:
        """Whether any granted scope allows ``resource:action``."""
        return any(s.allows(resource, action) for s in self._scopes)

    def require(self, resource: str, action: str) -> None:
        """Raise :class:`ValidationError` (code ``auth.forbidden``) unless ``resource:action`` is granted."""
        if not self.allows(resource, action):
            raise ValidationError(
                f"missing scope {resource}:{action}", code="auth.forbidden", resource=resource, action=action
            )

    def granted(self) -> list[str]:
        """The granted scopes as ``resource:action`` strings, sorted."""
        return sorted(str(s) for s in self._scopes)


def parse_scopes(raw: str) -> ScopeSet:
    """Parse a space- or comma-separated scope string into a :class:`ScopeSet`.

    Each token is ``resource:action``; a bare token ``x`` is read as ``x:*``. An empty string is an empty set.
    A malformed token (more than one colon) raises :class:`ValidationError`.
    """
    tokens = [t for t in raw.replace(",", " ").split() if t]
    scopes: list[Scope] = []
    for token in tokens:
        parts = token.split(":")
        if len(parts) == 1:
            scopes.append(Scope(parts[0], "*"))
        elif len(parts) == 2:
            scopes.append(Scope(parts[0], parts[1]))
        else:
            raise ValidationError(f"malformed scope token {token!r}", path="scopes")
    return ScopeSet(scopes)
