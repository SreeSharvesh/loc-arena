"""Trusted startup and lifecycle authority: principals, teams, resources, credentials. Not for agents."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from meridian_iam.contracts import (
    Forbidden,
    Invalid,
    Manifest,
)
from meridian_iam.core.base import OPERATOR_TEAM, Base


class Bootstrap(Base):
    """Seeds and maintains identity; called by the launcher or an operator, never by an agent."""

    # ---- bootstrap: trusted authority, never reachable by an agent -----------------------------------------

    def add_principal(self, principal: str, kind: str) -> None:
        """Register a stable principal (``agent``, ``job`` or ``service``)."""
        if kind not in {"agent", "job", "service"} or not principal:
            raise Invalid("invalid principal")
        with self._tx() as db:
            db.execute("INSERT INTO principals(id, kind) VALUES (?, ?)", (principal, kind))

    def add_member(
        self,
        principal: str,
        team: str,
        role: str,
        authority: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        """Make ``principal`` a member of ``team``.

        ``authority`` maps each resource to the permissions the member may grant on it: standing authority is
        per resource, so a right to grant ``read`` on one resource gives none on another.
        """
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO memberships VALUES (?, ?, ?, ?, 1, 'bootstrap')",
                (principal, team, role, json.dumps({k: sorted(v) for k, v in (authority or {}).items()})),
            )

    def remove_member(self, principal: str, team: str) -> None:
        """End eligibility now. Approver authority goes at once; grants already issued stay independent."""
        with self._tx() as db:
            db.execute(
                "UPDATE memberships SET active = 0 WHERE principal = ? AND team = ?",
                (principal, team),
            )

    def register_resource(
        self,
        manifest: Manifest,
        *,
        owner_team: str,
        approver_role: str,
        max_seconds: int,
        allow_ongoing: bool = False,
        max_pending: int = 20,
    ) -> None:
        """Register a service's operation contract and its owner policy."""
        if owner_team == OPERATOR_TEAM:
            raise Invalid("the operator team is reserved: no resource can be owned by it")
        operations = {
            name: {
                "permission": op.permission,
                "requestable": op.requestable,
                "authenticated": op.authenticated,
                "public": op.public,
                "target": op.target,
                "launch": op.launch,
            }
            for name, op in manifest.operations.items()
        }
        with self._tx() as db:
            db.execute(
                "INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.resource,
                    owner_team,
                    approver_role,
                    max_seconds,
                    allow_ongoing,
                    max_pending,
                    json.dumps(operations),
                ),
            )

    def bind_service(self, service: str, resource: str, audience: str) -> None:
        """Bind a service principal to one resource and the audience its callers' credentials carry."""
        with self._tx() as db:
            db.execute("INSERT INTO service_bindings VALUES (?, ?, ?)", (service, resource, audience))

    def allow_delegation(self, issuer: str, delegable: Mapping[str, Iterable[str]]) -> None:
        """Let the service bound to resource ``issuer`` give workloads it issues these permissions.

        ``delegable`` maps each resource to the permissions on it the issuer may hand on, its own resource
        included only if listed. Nothing else widens it, and a service with no delegation cannot issue
        workloads at all, so a compromised service can mint no more than its seed allows. An unknown
        resource or permission is ``Invalid``.
        """
        with self._tx() as db:
            self._resource(db, issuer)
            for resource, permissions in delegable.items():
                operations = json.loads(self._resource(db, resource)["operations"]).values()
                wanted = set(permissions)
                if not wanted <= {op["permission"] for op in operations if op["permission"]}:
                    raise Invalid("unknown resource or permission")
                for permission in sorted(wanted):
                    db.execute(
                        "INSERT OR IGNORE INTO delegations VALUES (?, ?, ?)",
                        (issuer, resource, permission),
                    )

    def add_baseline_grant(self, principal: str, resource: str, permissions: Iterable[str]) -> None:
        """Seed an ongoing grant the run config already gives this principal (revocable)."""
        with self._tx() as db:
            self._resource(db, resource)
            self._insert_grants(
                db,
                principal,
                resource,
                sorted(set(permissions)),
                None,
                "baseline",
                None,
                "bootstrap",
            )

    def issue_credential(
        self,
        principal: str,
        audiences: Iterable[str],
        *,
        ttl: int | None = None,
        label: str = "",
    ) -> str:
        """Issue a credential (returned once; only its digest is stored). ``label`` names one of many."""
        with self._tx() as db:
            generation = self._generation(db, principal, label) + 1
            return self._insert_credential(db, principal, sorted(audiences), ttl, generation, label)

    def import_credential(
        self,
        principal: str,
        token: str,
        audiences: Iterable[str],
        *,
        label: str = "",
        ttl: int | None = None,
    ) -> None:
        """Register a credential delivered out of band (a mounted secret): only its digest is stored."""
        with self._tx() as db:
            generation = self._generation(db, principal, label) + 1
            self._insert_credential(db, principal, sorted(audiences), ttl, generation, label, token=token)

    def rotate_credential(self, principal: str, *, label: str = "", ttl: int | None = None) -> str:
        """Replace one labelled credential's generation: old tokens fail at once; grants stay."""
        with self._tx() as db:
            row = db.execute("SELECT active FROM principals WHERE id = ?", (principal,)).fetchone()
            if row is None or not row["active"]:
                raise Forbidden("principal cannot be rotated")
            latest = db.execute(
                "SELECT audiences FROM credentials WHERE principal = ? AND label = ? "
                "ORDER BY generation DESC LIMIT 1",
                (principal, label),
            ).fetchone()
            if latest is None:
                raise Invalid("no credential to rotate")
            db.execute(
                "UPDATE credentials SET revoked_at = ? WHERE principal = ? AND label = ? "
                "AND revoked_at IS NULL",
                (self._now(), principal, label),
            )
            generation = self._generation(db, principal, label) + 1
            audiences = json.loads(latest["audiences"])
            return self._insert_credential(db, principal, audiences, ttl, generation, label)

    def disable_principal(self, principal: str) -> None:
        """Deny everything for this principal now; grants are kept but unusable; rotation cannot revive it."""
        with self._tx() as db:
            db.execute("UPDATE principals SET active = 0 WHERE id = ?", (principal,))
