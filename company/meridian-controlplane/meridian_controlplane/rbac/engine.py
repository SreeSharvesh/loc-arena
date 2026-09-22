"""The RBAC engine: decide whether a role may perform an action on a resource.

:class:`RbacEngine` evaluates a required ``resource:action`` against every scope a :class:`Role` grants. A
scope grants access when its resource matches the required resource and its action matches the required
action. Resource matching supports the global wildcard ``*``, namespace scopes of the form ``ns.*`` that
cover resources under a namespace, and exact resources. Action matching supports the wildcard ``*`` and exact
actions.
"""

from __future__ import annotations

from meridian_common.authclient import Scope
from meridian_controlplane.errors import RbacError
from meridian_controlplane.rbac.roles import Role


class RbacEngine:
    """Evaluates access decisions for roles over resources and actions."""

    def _action_matches(self, granted: str, required: str) -> bool:
        """Whether a granted action covers the required action."""
        return granted == "*" or granted == required

    def _resource_matches(self, granted: str, required: str) -> bool:
        """Whether a granted resource covers the required resource, honoring namespace scopes."""
        if granted == "*":
            return True
        if granted.endswith(".*"):
            prefix = granted[:-2]
            return required == prefix or required.startswith(prefix)
        return granted == required

    def _grants(self, scope: Scope, resource: str, action: str) -> bool:
        """Whether a single scope grants ``resource:action``."""
        return self._resource_matches(scope.resource, resource) and self._action_matches(scope.action, action)

    def can(self, role: Role, resource: str, action: str) -> bool:
        """Whether ``role`` may perform ``action`` on ``resource``."""
        if not resource or not action:
            raise RbacError("resource and action must be non-empty", path="access")
        return any(self._grants(scope, resource, action) for scope in role.scopes)

    def require(self, role: Role, resource: str, action: str) -> None:
        """Raise :class:`RbacError` unless ``role`` may perform ``action`` on ``resource``."""
        if not self.can(role, resource, action):
            raise RbacError(
                f"role {role.name!r} may not {action} {resource}",
                code="controlplane.rbac",
                resource=resource,
                action=action,
            )
