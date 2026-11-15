"""A release's operation contract (spike): staged by the deployer, active once its release serves.

The deployer, and only it, stages the manifest a release carries for a resource it deploys. IAM refuses a
contract that would decide a call differently for any operation the active one already has: each kept
operation must be unchanged, and each new one must need a permission the resource already has, so that the
permission's holders, whom IAM already knows, are its callers. A staged contract becomes the resource's
operations when the deployer activates it, once the release serves; a release that does not serve has it
discarded, and the active one stays.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from meridian_iam.contracts import Forbidden, Invalid, Manifest
from meridian_iam.core.base import IAM_AUDIENCE, Base

STAGED, ACTIVE_STATE, DISCARDED = "staged", "active", "discarded"


class Contracts(Base):
    """Staging, activation and discard of a release's contract, by the resource's deployer alone."""

    def allow_staging(self, principal: str, resources: Iterable[str]) -> None:
        """Bootstrap: let ``principal``, the deployer, stage contracts for each of ``resources``."""
        with self._tx() as db:
            for resource in resources:
                self._resource(db, resource)
                db.execute("INSERT OR IGNORE INTO stagers VALUES (?, ?)", (principal, resource))

    def stage_contract(self, token: str, manifest: Manifest, deployment: str) -> dict[str, Any]:
        """Stage ``manifest`` for ``deployment``; what it adds and removes, or ``Forbidden`` saying why."""
        with self._tx() as db:
            actor = self._stager(db, token, manifest.resource)
            active = json.loads(self._resource(db, manifest.resource)["operations"])
            staged = _operations(manifest)
            change = _check(active, staged)
            db.execute(
                "INSERT OR REPLACE INTO contracts VALUES (?, ?, ?, ?, ?, ?)",
                (manifest.resource, deployment, json.dumps(staged), STAGED, actor, self._now()),
            )
            self._audit(
                db,
                "contract_staged",
                actor,
                resource=manifest.resource,
                deployment=deployment,
                **change,
            )
            return change

    def activate_contract(self, token: str, resource: str, deployment: str) -> None:
        """Make the contract staged for ``deployment`` the operations IAM decides ``resource``'s calls by."""
        with self._tx() as db:
            actor = self._stager(db, token, resource)
            operations = self._staged(db, resource, deployment)
            db.execute("UPDATE resources SET operations = ? WHERE id = ?", (operations, resource))
            db.execute(
                "UPDATE contracts SET state = CASE WHEN deployment = ? THEN ? ELSE state END "
                "WHERE resource = ? AND state IN (?, ?)",
                (deployment, ACTIVE_STATE, resource, ACTIVE_STATE, STAGED),
            )
            self._audit(db, "contract_activated", actor, resource=resource, deployment=deployment)

    def discard_contract(self, token: str, resource: str, deployment: str) -> None:
        """Drop the contract staged for ``deployment``: its release did not serve; the active one stays."""
        with self._tx() as db:
            actor = self._stager(db, token, resource)
            self._staged(db, resource, deployment)
            db.execute(
                "UPDATE contracts SET state = ? WHERE resource = ? AND deployment = ?",
                (DISCARDED, resource, deployment),
            )
            self._audit(db, "contract_discarded", actor, resource=resource, deployment=deployment)

    def _stager(self, db: sqlite3.Connection, token: str, resource: str) -> str:
        actor = self._auth(db, token, IAM_AUDIENCE)
        allowed = db.execute("SELECT 1 FROM stagers WHERE principal = ? AND resource = ?", (actor, resource))
        if allowed.fetchone() is None:
            raise Forbidden(f"{actor} may not stage a contract for {resource}: its owners deploy it")
        return actor

    def _staged(self, db: sqlite3.Connection, resource: str, deployment: str) -> str:
        row = db.execute(
            "SELECT operations FROM contracts WHERE resource = ? AND deployment = ? AND state = ?",
            (resource, deployment, STAGED),
        ).fetchone()
        if row is None:
            raise Invalid(f"no contract of {resource} is staged for {deployment}")
        return row["operations"]


def _operations(manifest: Manifest) -> dict[str, dict[str, Any]]:
    """The operations of ``manifest`` as the resources table keeps them."""
    return {
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


def _check(active: dict[str, dict[str, Any]], staged: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """What ``staged`` adds and removes against ``active``; ``Forbidden`` if it widens or changes anything."""
    permissions = sorted({op["permission"] for op in active.values() if op["permission"]})
    kept = sorted(active.keys() & staged.keys())
    problems = [f"{name} changes" for name in kept if active[name] != staged[name]]
    added = {name: staged[name] for name in sorted(staged.keys() - active.keys())}
    for name, op in added.items():
        if op["public"] or op["authenticated"] or op["launch"]:
            problems.append(f"{name} is public, authenticated or a launch: a release adds none of those")
        elif op["permission"] not in permissions:
            problems.append(f"{name} needs {op['permission']}; the resource's are {', '.join(permissions)}")
    if problems:
        refusal = "a release's contract may only add operations under existing permissions: "
        raise Forbidden(refusal + "; ".join(problems))
    return {
        "added": {name: op["permission"] for name, op in added.items()},
        "removed": sorted(active.keys() - staged.keys()),
    }
