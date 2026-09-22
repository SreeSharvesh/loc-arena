"""Roles and permission evaluation over the shared scope grammar."""

from __future__ import annotations

from meridian_controlplane.rbac.engine import RbacEngine
from meridian_controlplane.rbac.roles import Role

__all__ = ["RbacEngine", "Role"]
