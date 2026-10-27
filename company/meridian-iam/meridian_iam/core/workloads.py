"""Workload identities: a resource service issues a job or agent run its own, narrower principal."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from meridian_iam.contracts import Conflict, Forbidden, Invalid, Workload
from meridian_iam.core.base import ACTIVE, IAM_AUDIENCE, SCOPE_PREFIX, Base, digest
from meridian_iam.core.lifecycle import REPLACEMENT


class Workloads(Base):
    """Runtime issuance of execution principals: the one way a principal is created after the seed."""

    def delegable(self, service_token: str, caller_token: str | None, operation: str) -> dict[str, list[str]]:
        """What work the caller launches through ``operation`` could be given now, by resource.

        Each permission this service may delegate (``allow_delegation``) that the caller holds now: exactly
        what ``issue_workload`` would grant it, so the service can ask for that. The gate is issuance's: the
        service holds a binding, the caller's credential carries the binding's audience, and ``operation`` is
        one the service's manifest marks ``launch`` whose permission the caller holds; otherwise
        ``Unauthenticated`` or ``Forbidden``. It changes nothing.
        """
        with self._tx() as db:
            binding = self._binding(db, self._auth(db, service_token, IAM_AUDIENCE))
            sponsor = self._auth(db, caller_token, binding["audience"])
            self._launch_permission(db, sponsor, binding["resource"], operation)
            held: dict[str, list[str]] = {}
            for row in db.execute(
                "SELECT resource, permission FROM delegations WHERE issuer = ? ORDER BY resource, permission",
                (binding["resource"],),
            ):
                if self._holds(db, sponsor, row["resource"], row["permission"]):
                    held.setdefault(row["resource"], []).append(row["permission"])
            return held

    def _launch_permission(self, db: sqlite3.Connection, sponsor: str, resource: str, operation: str) -> None:
        """``Forbidden`` unless ``operation`` is a launch operation whose permission ``sponsor`` holds."""
        launch = json.loads(self._resource(db, resource)["operations"]).get(operation)
        if not (launch and launch.get("launch")) or not self._holds(
            db,
            sponsor,
            resource,
            launch["permission"],
        ):
            raise Forbidden("not allowed to launch work through this service")

    def issue_workload(  # noqa: PLR0913, C901 - C901 counts its nested functions, each checked on its own
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
        wanted = {resource: sorted(set(permissions)) for resource, permissions in grants.items()}
        payload = {
            "operation": operation,
            "reference": reference,
            "purpose": purpose,
            "grants": wanted,
            "ttl": ttl,
        }

        def run(db: sqlite3.Connection, issuer: str) -> dict[str, Any]:  # noqa: C901 - predates the complexity gate
            binding = self._binding(db, issuer)
            sponsor = self._auth(db, caller_token, binding["audience"])
            if not all(
                isinstance(text, str) and 0 < len(text.strip()) <= 200 for text in (reference, purpose)
            ):
                raise Invalid("a reference and a purpose of 1-200 characters are required")
            work, said = reference.strip(), purpose.strip()  # stored and returned as checked
            if not wanted or not all(wanted.values()):
                raise Invalid("a workload needs at least one grant")
            if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
                raise Invalid("a positive ttl is required")
            self._launch_permission(db, sponsor, binding["resource"], operation)
            now = self._now()
            ends: dict[tuple[str, str], int] = {}
            for resource, permissions in wanted.items():
                self._lifetime(self._resource(db, resource), now + ttl)
                for permission in permissions:
                    delegable = db.execute(
                        "SELECT 1 FROM delegations WHERE issuer = ? AND resource = ? AND permission = ?",
                        (binding["resource"], resource, permission),
                    ).fetchone()
                    if delegable is None or not self._holds(db, sponsor, resource, permission):
                        raise Forbidden(
                            "a workload gets only what its service delegates and its sponsor holds",
                        )
                    covering = [
                        row["expires_at"]
                        for row in db.execute(
                            f"SELECT expires_at FROM grants WHERE principal = :p AND resource = :r "
                            f"AND permission = :perm AND {ACTIVE}",
                            {"p": sponsor, "r": resource, "perm": permission, "now": now},
                        )
                    ]
                    ends[resource, permission] = (
                        now + ttl if None in covering else min(now + ttl, max(covering))
                    )
            marks = ",".join("?" * len(wanted))
            needed = {
                row["audience"]
                for row in db.execute(
                    f"SELECT audience FROM service_bindings WHERE resource IN ({marks})",
                    tuple(wanted),
                )
            }
            if needed & {IAM_AUDIENCE} or any(audience.startswith(SCOPE_PREFIX) for audience in needed):
                raise Invalid("a workload cannot get an audience that manages IAM")
            if set(credentials) != needed or not all(
                isinstance(secret, str) and REPLACEMENT.fullmatch(secret) for secret in credentials.values()
            ):
                raise Invalid("one secret of 32-128 URL-safe characters per audience the grants need")
            digests = [digest(secret) for secret in credentials.values()]
            marks = ",".join("?" * len(digests))
            if (
                len(set(digests)) < len(digests)
                or db.execute(
                    f"SELECT 1 FROM credentials WHERE digest IN ({marks})",
                    digests,
                ).fetchone()
            ):
                raise Conflict("that secret is already in use")
            principal = f"job-{self._id()}"
            horizon = db.execute("SELECT COALESCE(MAX(id), 0) FROM grants").fetchone()[0]
            db.execute("INSERT INTO principals(id, kind) VALUES (?, 'job')", (principal,))
            db.execute(
                "INSERT INTO workloads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    principal,
                    binding["resource"],
                    issuer,
                    sponsor,
                    operation,
                    work,
                    said,
                    now,
                    now + ttl,
                    horizon,
                ),
            )
            for (resource, permission), end in ends.items():
                self._insert_grants(db, principal, resource, [permission], end, "workload", None, sponsor)
            for audience, secret in sorted(credentials.items()):
                self._insert_credential(db, principal, [audience], ttl, 1, audience, token=secret, now=now)
            return {
                "principal": principal,
                "resource": binding["resource"],
                "sponsor": sponsor,
                "reference": work,
                "purpose": said,
                "grants": wanted,
                "expires_at": now + ttl,
            }

        bearer = {"caller": caller_token or "", **{f"credential:{a}": s for a, s in credentials.items()}}
        return Workload(
            **self._mutate(service_token, "workload_issued", key, payload, run, secret_inputs=bearer),
        )
