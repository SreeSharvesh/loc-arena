"""Control-plane errors, extending the shared Meridian hierarchy.

The control plane raises these so callers can tell a policy fault from a rollout fault from an RBAC,
identity-config, CI-config, or scaffold-config fault. They all derive from
:class:`meridian_common.errors.MeridianError`, so a generic handler still catches them, while the stable
``code`` keys alerts on the specific surface.
"""

from __future__ import annotations

from meridian_common.errors import ConfigError, MeridianError, ValidationError


class PolicyError(ValidationError):
    """A monitor-policy change or coverage set was malformed or failed validation."""

    code = "controlplane.policy"


class RolloutError(MeridianError):
    """A deployment rollout could not be planned or reconciled from the given spec and state."""

    code = "controlplane.rollout"


class RbacError(ValidationError):
    """A role or permission definition was malformed, or an access check was given invalid input."""

    code = "controlplane.rbac"


class IdentityConfigError(ConfigError):
    """A service-account, rotation, or issuance policy definition was invalid."""

    code = "controlplane.identity"


class CiConfigError(ConfigError):
    """A CI pipeline definition was malformed: an empty pipeline, an unknown stage, or a duplicate stage."""

    code = "controlplane.ci"


class ScaffoldConfigError(ConfigError):
    """A scaffold configuration was invalid: a duplicate role, a non-positive cap, or an empty roster."""

    code = "controlplane.scaffold"
