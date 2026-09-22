"""Roles: a named bundle of granted scopes.

A :class:`Role` binds a name to a set of granted scopes expressed in the platform scope grammar and parsed
through ``meridian_common.authclient``. Scopes may be plain (``policy:read``), action-wildcarded
(``policy:*``), globally wildcarded (``*``), or namespaced (``monitor.*:read`` grants ``read`` on resources
under the ``monitor`` namespace). The engine in :mod:`meridian_controlplane.rbac.engine` evaluates access
against a role's scopes.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common.authclient import Scope, ScopeSet, parse_scopes


def _scope_from_token(token: str) -> Scope:
    """Rebuild a :class:`~meridian_common.authclient.Scope` from a normalized ``resource:action`` token."""
    resource, action = token.split(":", 1)
    return Scope(resource=resource, action=action)


@dataclass(frozen=True)
class Role:
    """A named role: its granted scopes as a tuple of :class:`~meridian_common.authclient.Scope`."""

    name: str
    scopes: tuple[Scope, ...]

    @classmethod
    def of(cls, name: str, scope_string: str) -> Role:
        """Build a role named ``name`` from a scope string (parsed via the shared scope grammar)."""
        granted = parse_scopes(scope_string).granted()
        return cls(name=name, scopes=tuple(_scope_from_token(token) for token in granted))

    def scope_set(self) -> ScopeSet:
        """The role's scopes as a :class:`~meridian_common.authclient.ScopeSet`."""
        return ScopeSet(list(self.scopes))

    def granted(self) -> tuple[str, ...]:
        """The role's scopes as ``resource:action`` strings, sorted."""
        return tuple(sorted(str(scope) for scope in self.scopes))
