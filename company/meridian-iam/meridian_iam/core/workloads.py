"""Workload identities: a resource service issues a job or agent run its own, narrower principal."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from meridian_iam.contracts import Workload
from meridian_iam.core.base import Base


class Workloads(Base):
    """Runtime issuance of execution principals: the one way a principal is created after the seed."""

    def issue_workload(  # noqa: PLR0913
        self,
        service_token: str,
        caller_token: str | None,
        operation: str,
        reference: str,
        grants: Mapping[str, Iterable[str]],
        ttl: int,
        credential: str,
        key: str,
    ) -> Workload:
        """Create the ``job`` principal for work a caller launches through this service.

        The service authenticates with its ``iam`` credential and must hold a service binding. The caller's
        credential must carry that binding's audience, and ``operation`` (the launching operation in the
        service's manifest, such as ``submit_job``) must be allowed for the caller exactly as ``authorize``
        decides it. Every (resource, permission) in ``grants`` must be an active grant of the caller's: the
        workload gets that subset and nothing more, never the caller's credential or all of its grants.

        ``ttl`` is required and positive. The workload's credential and grants expire at ``now + ttl``, cut
        short to the earliest expiry of any caller grant they copy; a ``ttl`` beyond a granted resource's
        ``max_seconds`` is ``Invalid``. ``credential`` is a secret the service generated and hands to the
        workload: IAM stores only its digest, with the audiences the granted resources' services require, and
        never returns it. ``reference`` is the service's own ID for the work (a job or run ID).

        Idempotent per (service, ``key``): a retry with the same content returns the same workload, and the
        same key with any other content, the secret included, is ``Conflict``. A bad service or caller
        credential is ``Unauthenticated``; a disallowed operation or a grant the caller does not hold is
        ``Forbidden``; a bad ``ttl``, empty ``grants`` or an unknown resource is ``Invalid``. A rejected call
        changes nothing and is audited; success is audited as ``workload_issued``, without the secret.
        """
        raise NotImplementedError
