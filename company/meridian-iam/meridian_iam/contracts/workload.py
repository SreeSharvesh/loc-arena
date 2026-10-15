"""The typed answer IAM returns when a resource service issues an identity for work it launches."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Workload:
    """A job or agent run's own principal: never its sponsor's credential, and it carries no bearer material.

    ``resource`` is the resource whose service issued it, ``sponsor`` the caller who launched the work,
    ``reference`` the service's own ID for that work, and ``grants`` maps each resource to the permissions the
    workload holds on it until ``expires_at``.
    """

    principal: str
    resource: str
    sponsor: str
    reference: str
    grants: dict[str, list[str]]
    expires_at: int
