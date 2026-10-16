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
        purpose: str,
        grants: Mapping[str, Iterable[str]],
        ttl: int,
        credentials: Mapping[str, str],
        key: str,
    ) -> Workload:
        """Create the ``job`` principal for work a caller launches through this service.

        Who may issue: the service authenticates with its ``iam`` credential and must hold a service binding.
        The caller's credential must carry that binding's audience, ``operation`` must be one the service's
        manifest marks ``launch``, and the caller must hold its permission.

        What the workload gets: every (resource, permission) in ``grants`` must be one this service may
        delegate (``allow_delegation``) and one the caller holds now. The workload gets exactly that subset,
        never the caller's credential or all of its grants. Each copied grant ends at ``now + ttl`` or when
        the caller's grant it copies ends, whichever is first; a ``ttl`` beyond a granted resource's
        ``max_seconds`` is ``Invalid``.

        Its credentials: ``credentials`` maps each audience the granted resources' services require to a
        secret the service generated, one per audience, so each service the workload calls sees only its own
        secret. IAM stores only their digests, labelled by audience and expiring at ``now + ttl``, never
        returns them, and gives none the ``iam`` audience. ``reference`` is the service's own ID for the work
        and ``purpose`` what the service says it is for (an eval run, CI, an inference server): IAM records
        both for whoever later checks the work against them, and judges neither.

        While it lives: a workload's grant counts at ``authorize`` only while its issuer is active, still
        bound to the same resource and still allowed to delegate that permission, and its sponsor is active
        and holds the permission through a grant it already had when the workload was issued, and so on up any
        chain of workloads that launched workloads. Revoking the sponsor's grant or disabling the
        sponsor or the issuer stops the work at once, and granting the sponsor the permission again later
        does not revive it.

        Idempotent per (service, ``key``): a retry with the same content, the caller's credential and the
        secrets included, returns the same workload, and the same key with any other content is ``Conflict``,
        so a reused key can never hand one caller's workload to another. A bad service or caller credential
        is ``Unauthenticated``; a launch the caller may not make, or a grant the service may not delegate or
        the caller does not hold, is ``Forbidden``; a bad ``ttl``, an empty ``reference``, ``purpose`` or
        ``grants``, an unknown resource, or ``credentials`` that do not cover exactly the audiences needed is
        ``Invalid``; a secret already in use is ``Conflict``. A rejected call changes nothing and is audited;
        success is audited as ``workload_issued``, without the secrets.
        """
        raise NotImplementedError
