"""A release's operation contract: staged by the deployer, active once its release serves.

The deployer, and only it, stages the manifest a release carries for a resource it deploys. IAM checks each
contract against the resource's base contract, the one registered at bootstrap from the image, so no order
of releases can widen what IAM decides: every operation the base has is either dropped or kept unchanged,
and every other operation needs a permission the base has, is requestable only where the base already makes
that permission requestable, and is neither public, authenticated nor a launch. A staged contract becomes
the resource's operations when the deployer activates it, once the release serves; a release that does not
serve has it discarded, and the active one stays. Each step, a refusal included, is an audit line.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypedDict

from meridian_iam.contracts import (
    ContractChange,
    Forbidden,
    Invalid,
    Manifest,
    ManifestError,
    Unauthenticated,
    load_manifest,
)
from meridian_iam.core.base import IAM_AUDIENCE, Base

BASE, STAGED, ACTIVE, SUPERSEDED, DISCARDED = "base", "staged", "active", "superseded", "discarded"
IMAGE = "image"  # the deployment name of a resource's base contract


class StoredOperation(TypedDict):
    """An operation as IAM keeps it, in the resources table and in each contract."""

    permission: str | None
    requestable: bool
    authenticated: bool
    public: bool
    target: str | None
    launch: bool


Operations = dict[str, StoredOperation]


class Contracts(Base):
    """Staging, activation and discard of a release's contract, by the resource's deployer alone."""

    def allow_staging(self, principal: str, resources: Iterable[str]) -> None:
        """Bootstrap: let ``principal``, the deployer, stage contracts for each of ``resources``."""
        with self._tx() as db:
            for resource in resources:
                self._resource(db, resource)
                db.execute("INSERT OR IGNORE INTO stagers VALUES (?, ?)", (principal, resource))

    def stage_contract(self, token: str, document: Mapping[str, Any], deployment: str) -> ContractChange:
        """Stage the manifest ``document`` for ``deployment``: what it adds, changes and removes.

        ``Forbidden`` says why IAM refuses it; a malformed manifest, or a deployment already staged once, is
        ``Invalid``. Either refusal is audited.
        """
        with self._refusals_audited(token, "stage", None, deployment), self._tx() as db:
            try:
                manifest = load_manifest(document)
            except ManifestError as error:
                raise Invalid(f"the manifest is malformed: {error}") from None
            resource = manifest.resource
            actor = self._stager(db, token, resource, "stage")
            staged = operations_of(manifest)
            _check(self._base(db, resource), staged)
            active: Operations = json.loads(self._resource(db, resource)["operations"])
            change: ContractChange = {
                "added": {
                    name: op["permission"] for name, op in sorted(staged.items()) if name not in active
                },
                "changed": {
                    name: op["permission"]
                    for name, op in sorted(staged.items())
                    if active.get(name, op) != op
                },
                "removed": sorted(active.keys() - staged.keys()),
            }
            try:
                db.execute(
                    "INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?)",
                    (resource, deployment, json.dumps(staged), STAGED, actor, self._now()),
                )
            except sqlite3.IntegrityError:
                raise Invalid(f"a contract of {resource} was staged for {deployment} already") from None
            self._audit(db, "contract_staged", actor, resource=resource, deployment=deployment, **change)
            return change

    def activate_contract(self, token: str, resource: str, deployment: str) -> None:
        """Make the contract staged for ``deployment`` the operations IAM decides ``resource``'s calls by.

        Activating it again while it is active changes nothing, so a lost reply can be asked again.
        """
        with self._refusals_audited(token, "activate", resource, deployment), self._tx() as db:
            actor = self._stager(db, token, resource, "activate")
            if self._state(db, resource, deployment) == ACTIVE:
                return
            operations = self._staged(db, resource, deployment)
            db.execute("UPDATE resources SET operations = ? WHERE id = ?", (operations, resource))
            db.execute(
                "UPDATE contracts SET state = ? WHERE resource = ? AND state = ?",
                (SUPERSEDED, resource, ACTIVE),
            )
            self._set_state(db, resource, deployment, ACTIVE)
            self._audit(db, "contract_activated", actor, resource=resource, deployment=deployment)

    def discard_contract(self, token: str, resource: str, deployment: str) -> None:
        """Drop the contract staged for ``deployment``: its release did not serve; the active one stays.

        Discarding it again changes nothing.
        """
        with self._refusals_audited(token, "discard", resource, deployment), self._tx() as db:
            actor = self._stager(db, token, resource, "discard")
            if self._state(db, resource, deployment) == DISCARDED:
                return
            self._staged(db, resource, deployment)
            self._set_state(db, resource, deployment, DISCARDED)
            self._audit(db, "contract_discarded", actor, resource=resource, deployment=deployment)

    @contextmanager
    def _refusals_audited(
        self,
        token: str,
        step: str,
        resource: str | None,
        deployment: str,
    ) -> Iterator[None]:
        """Audit a refusal of ``step``, once its transaction has rolled back, naming the caller if it can."""
        try:
            yield
        except (Forbidden, Invalid) as refused:
            with self._tx() as db:
                try:
                    actor = self._auth(db, token, IAM_AUDIENCE)
                except Unauthenticated:  # the credential lapsed since: the refusal is audited all the same
                    actor = None
                self._audit(
                    db,
                    "contract_refused",
                    actor,
                    step=step,
                    resource=resource,
                    deployment=deployment,
                    reason=str(refused),
                )
            raise

    def _stager(self, db: sqlite3.Connection, token: str, resource: str, step: str) -> str:
        actor = self._auth(db, token, IAM_AUDIENCE)
        allowed = db.execute("SELECT 1 FROM stagers WHERE principal = ? AND resource = ?", (actor, resource))
        if allowed.fetchone() is None:
            raise Forbidden(f"{actor} may not {step} a contract for {resource}: its owners deploy it")
        return actor

    def _base(self, db: sqlite3.Connection, resource: str) -> Operations:
        """The resource's base contract, registered at bootstrap or by the store's upgrade."""
        row = db.execute(
            "SELECT operations FROM contracts WHERE resource = ? AND state = ?",
            (resource, BASE),
        )
        found = row.fetchone()
        if found is None:
            raise Invalid(f"{resource} has no base contract")
        return json.loads(found["operations"])

    def _staged(self, db: sqlite3.Connection, resource: str, deployment: str) -> str:
        row = db.execute(
            "SELECT operations FROM contracts WHERE resource = ? AND deployment = ? AND state = ?",
            (resource, deployment, STAGED),
        ).fetchone()
        if row is None:
            raise Invalid(f"no contract of {resource} is staged for {deployment}")
        return row["operations"]

    def _state(self, db: sqlite3.Connection, resource: str, deployment: str) -> str | None:
        row = db.execute(
            "SELECT state FROM contracts WHERE resource = ? AND deployment = ?",
            (resource, deployment),
        )
        found = row.fetchone()
        return None if found is None else found["state"]

    def _set_state(self, db: sqlite3.Connection, resource: str, deployment: str, state: str) -> None:
        db.execute(
            "UPDATE contracts SET state = ? WHERE resource = ? AND deployment = ?",
            (state, resource, deployment),
        )


def operations_of(manifest: Manifest) -> Operations:
    """The operations of ``manifest`` as IAM keeps them, in the resources table and in each contract."""
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


def _check(base: Operations, staged: Operations) -> None:
    """``Forbidden``, saying why, if ``staged`` changes a base operation or adds one that widens anything."""
    problems = [
        f"{name} changes" for name in sorted(base.keys() & staged.keys()) if base[name] != staged[name]
    ]
    problems += [
        problem
        for name, op in sorted(staged.items())
        if name not in base and (problem := _widening(base, name, op))
    ]
    if problems:
        refusal = "a release's contract may only add operations under existing permissions: "
        raise Forbidden(refusal + "; ".join(problems))


def _widening(base: Operations, name: str, op: StoredOperation) -> str | None:
    """Why the new operation ``name`` widens what ``base`` lets IAM decide, if it does."""
    permissions = sorted({held["permission"] for held in base.values() if held["permission"]})
    if op["public"] or op["authenticated"] or op["launch"]:
        return f"{name} is public, authenticated or a launch: a release adds none of those"
    if op["permission"] not in permissions:
        return f"{name} needs {op['permission']}; the resource's are {', '.join(permissions)}"
    if op["requestable"] and not any(
        held["permission"] == op["permission"] and held["requestable"] for held in base.values()
    ):
        return f"{name} makes {op['permission']} requestable, which it is not"
    return None
