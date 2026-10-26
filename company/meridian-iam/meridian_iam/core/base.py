"""What every part of the IAM core shares: the store, one write transaction, authentication, audit."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from meridian_iam.contracts import (
    Conflict,
    Forbidden,
    IamError,
    Invalid,
    Unauthenticated,
)
from meridian_iam.core.store import connect, transaction

OPERATOR_TEAM = "iam-operators"  # reserved: only a bootstrap seed makes a member of it
OPERATOR_ROLE = "operator"
IAM_AUDIENCE = "iam"  # a credential with it manages every resource IAM holds
SCOPE_PREFIX = "iam:"  # ``iam:<resource>`` manages that resource alone: what a resource service is handed
# ``iam-inbox`` reads its holder's inbox and nothing else: no management path accepts it (it is not ``iam``
# or ``iam:<resource>``), so the tools gateway that delivers the inbox never holds a credential managing IAM.
INBOX_AUDIENCE = "iam-inbox"
ACTIVE = "revoked_at IS NULL AND starts_at <= :now AND (expires_at IS NULL OR :now < expires_at)"


def digest(token: str) -> str:
    """The SHA-256 of a bearer token: the only form of it the store keeps."""
    return hashlib.sha256(token.encode()).hexdigest()


FIELD_LIMIT = 500  # characters of each field of a refused call's payload to audit: it was never validated
_LINE_KEYS = frozenset({"operation", "code", "via"})  # the rejected line's own fields


def _attempted(payload: dict[str, Any]) -> dict[str, Any]:
    """What a refused mutation tried, for its audit line, each field cut to ``FIELD_LIMIT`` characters.

    The payload holds no bearer material (that travels as ``secret_inputs``), but a refused one was never
    validated, so each field is bounded before it reaches the audit log and the monitors that read it, and
    one long field cannot hide the rest. A field named like one of the line's own keeps its value as
    ``attempted_<name>``.
    """
    tried = {}
    for name, value in payload.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        bounded = value if len(text) <= FIELD_LIMIT else f"{text[:FIELD_LIMIT]}… ({len(text)} characters)"
        tried[f"attempted_{name}" if name in _LINE_KEYS else name] = bounded
    return tried


class Base:
    """One authoritative IAM instance over one SQLite file. ``clock`` returns integer epoch seconds."""

    def __init__(
        self,
        path: Path | str,
        clock: Callable[[], int],
        *,
        token_factory: Callable[[], str] = lambda: "mi_" + secrets.token_urlsafe(32),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        renewal_grace_seconds: int = 60,
    ) -> None:
        """Open the store; the clock, generators and audit sink are injectable.

        ``audit_sink`` receives each audit event once its transaction has committed (never a rolled-back one).
        """
        self._db = connect(path)
        self._sink = audit_sink
        self.renewal_grace_seconds = renewal_grace_seconds
        self._pending: list[dict[str, Any]] = []
        self._depth = 0
        self._now = clock
        self._token = token_factory
        self._id = id_factory

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction whose audit events reach the sink only if it commits.

        Re-entrant: inside another transaction it joins it, so a multi-step seed commits or rolls back whole.
        """
        if self._depth:
            self._depth += 1
            try:
                yield self._db
            finally:
                self._depth -= 1
            return
        self._pending = []
        self._depth = 1
        try:
            with transaction(self._db) as db:
                yield db
        finally:
            self._depth = 0
        pending, self._pending = self._pending, []
        for event in pending:
            if self._sink is not None:
                self._sink(event)

    @contextmanager
    def seeding(self) -> Iterator[None]:
        """Seed the store in one transaction and mark it complete only if every step succeeded."""
        with self._tx() as db:
            yield
            db.execute("INSERT OR REPLACE INTO meta VALUES ('bootstrapped', '1')")

    def bootstrapped(self) -> bool:
        """Whether a seed ran to completion: a restart keeps its state; a half-made seed does not count."""
        return self._db.execute("SELECT 1 FROM meta WHERE key = 'bootstrapped'").fetchone() is not None

    def close(self) -> None:
        """Close the store."""
        self._db.close()

    def _live(self, db: sqlite3.Connection, token: str | None) -> sqlite3.Row:
        """The live credential row ``token`` is: not revoked, not expired, its principal active.

        A workload's principal is live only while every issuer and sponsor up its chain is too.
        """
        row = db.execute(
            "SELECT c.principal, c.audiences, c.label, c.generation, c.issued_at, c.expires_at, "
            "c.revoked_at, c.superseded_by, p.active, p.kind FROM credentials c "
            "JOIN principals p ON p.id = c.principal WHERE c.digest = ?",
            (digest(token or ""),),
        ).fetchone()
        now = self._now()
        if (
            row is None
            or row["revoked_at"] is not None
            or not row["active"]
            or (row["expires_at"] is not None and now >= row["expires_at"])
            or (row["kind"] == "job" and not self._chain_live(db, row["principal"]))
        ):
            raise Unauthenticated("invalid credential")
        return row

    def _chain_live(self, db: sqlite3.Connection, principal: str) -> bool:
        """Whether every sponsor up a workload's chain is active, and every issuer active and still bound."""
        while workload := db.execute("SELECT * FROM workloads WHERE principal = ?", (principal,)).fetchone():
            live = db.execute(
                "SELECT 1 FROM principals AS sponsor, principals AS issuer "
                "JOIN service_bindings ON service_bindings.principal = issuer.id "
                "WHERE sponsor.id = :sponsor AND sponsor.active = 1 AND issuer.id = :issuer "
                "AND issuer.active = 1 AND service_bindings.resource = :origin",
                {
                    "sponsor": workload["sponsor"],
                    "issuer": workload["issuer"],
                    "origin": workload["resource"],
                },
            ).fetchone()
            if live is None:
                return False
            principal = workload["sponsor"]
        return True

    def _credential(self, db: sqlite3.Connection, token: str | None, audience: str) -> sqlite3.Row:
        """The live credential row ``token`` is, for ``audience``, or ``Unauthenticated``."""
        row = self._live(db, token)
        if audience not in json.loads(row["audiences"]):
            raise Unauthenticated("invalid credential")
        return row

    def _auth(self, db: sqlite3.Connection, token: str | None, audience: str) -> str:
        return self._credential(db, token, audience)["principal"]

    def _manager(
        self,
        db: sqlite3.Connection,
        token: str | None,
        resource: str | None,
    ) -> tuple[str, str | None]:
        """The principal ``token`` manages as, and the resource it is bound to (``None``: all of them).

        A credential with the ``iam`` audience manages everything its principal may. One with only ``iam:<r>``
        manages resource ``r`` and nothing else, so a resource service that holds its callers' credentials, or
        that is compromised, can exercise them for its own resource and no other, beyond issuing workloads
        within the delegations its seed lists.
        """
        row = self._live(db, token)
        audiences = json.loads(row["audiences"])
        if IAM_AUDIENCE in audiences:
            return row["principal"], None
        if resource is not None and f"{SCOPE_PREFIX}{resource}" in audiences:
            return row["principal"], resource
        raise Unauthenticated("invalid credential")

    def _scope(self, row: sqlite3.Row) -> set[str] | None:
        """The resources a credential is bound to, or ``None`` when it manages all of them."""
        audiences = json.loads(row["audiences"])
        if IAM_AUDIENCE in audiences:
            return None
        bound = {a.removeprefix(SCOPE_PREFIX) for a in audiences if a.startswith(SCOPE_PREFIX)}
        if not bound:
            raise Unauthenticated("invalid credential")
        return bound

    def _mutate(  # noqa: PLR0913
        self,
        token: str,
        kind: str,
        key: str,
        payload: dict[str, Any],
        fn: Callable[[sqlite3.Connection, str], dict[str, Any]],
        resource: str | None = None,
        *,
        secret_inputs: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Authenticate, run ``fn`` once per (actor, key); a rejected call changes nothing but is audited.

        ``resource`` is the one resource the call concerns; a credential bound to another resource is refused.
        Audit events say ``via`` that resource when the credential was bound to it (a resource service
        forwarding its caller), and nothing when the caller managed IAM directly. ``secret_inputs`` (named
        bearer material the call carries) are part of what a retry must repeat, as digests, but never audited.
        """
        if not isinstance(key, str) or not key:
            raise Invalid("an idempotency key is required")
        content: list[Any] = [kind, payload]
        if secret_inputs is not None:
            content.append({name: digest(value) for name, value in secret_inputs.items()})
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        actor: str | None = None
        via: str | None = None
        try:
            with self._tx() as db:
                actor, via = self._manager(db, token, resource)
                seen = db.execute(
                    "SELECT fingerprint, result FROM mutation_keys WHERE actor = ? AND key = ?",
                    (actor, key),
                ).fetchone()
                if seen:
                    if seen["fingerprint"] != fingerprint:
                        raise Conflict("idempotency key reused with different content")
                    return json.loads(seen["result"])
                result = fn(db, actor)
                db.execute(
                    "INSERT INTO mutation_keys VALUES (?, ?, ?, ?)",
                    (actor, key, fingerprint, json.dumps(result)),
                )
                self._audit(db, kind, actor, via=via, **payload, result=result)
                return result
        except IamError as exc:
            with self._tx() as db:
                # A caller that never authenticated writes nothing of its own into the audit log.
                tried = _attempted(payload) if actor is not None else {}
                self._audit(db, "rejected", actor, **tried, operation=kind, code=exc.code, via=via)
            raise

    def _audit(self, db: sqlite3.Connection, kind: str, actor: str | None, **detail: Any) -> None:  # noqa: ANN401
        encoded = json.dumps(detail, default=str, sort_keys=True)
        at = self._now()
        db.execute(
            "INSERT INTO audit(at, kind, actor, detail) VALUES (?, ?, ?, ?)",
            (at, kind, actor, encoded),
        )
        self._pending.append({"at": at, "kind": kind, "actor": actor, **json.loads(encoded)})

    def _holds(self, db: sqlite3.Connection, principal: str, resource: str, permission: str) -> bool:
        """Whether ``principal`` holds ``permission`` on ``resource`` now.

        It needs an active grant of its own. A workload also needs its issuer to be active, still bound to the
        resource it issued from and still allowed to delegate this permission, and its sponsor to be active
        and to hold the permission through a grant it already had when the workload was issued (at or below
        the workload's ``grant_horizon``), and so on up the chain of workloads that launched workloads. So a
        revoked or disabled sponsor, or a disabled issuer, stops everything below it, and a later grant to the
        sponsor revives nothing.
        """
        now, horizon = self._now(), None
        while True:
            held = db.execute(
                "SELECT kind FROM grants JOIN principals ON principals.id = grants.principal "
                "WHERE grants.principal = :p AND resource = :r AND permission = :perm AND active = 1 "
                f"AND {ACTIVE} AND (:horizon IS NULL OR grants.id <= :horizon)",
                {"p": principal, "r": resource, "perm": permission, "now": now, "horizon": horizon},
            ).fetchone()
            if held is None:
                return False
            if held["kind"] != "job":  # only a job principal can be a workload
                return True
            workload = db.execute("SELECT * FROM workloads WHERE principal = ?", (principal,)).fetchone()
            if workload is None:
                return True
            issuer = db.execute(
                "SELECT 1 FROM principals "
                "JOIN service_bindings ON service_bindings.principal = principals.id "
                "JOIN delegations ON delegations.issuer = service_bindings.resource "
                "WHERE principals.id = :issuer AND active = 1 AND service_bindings.resource = :origin "
                "AND delegations.resource = :r AND delegations.permission = :perm",
                {
                    "issuer": workload["issuer"],
                    "origin": workload["resource"],
                    "r": resource,
                    "perm": permission,
                },
            ).fetchone()
            if issuer is None:
                return False
            principal, horizon = workload["sponsor"], workload["grant_horizon"]

    def _binding(self, db: sqlite3.Connection, service: str) -> sqlite3.Row:
        """The resource and audience ``service`` is bound to; ``Unauthenticated`` if it is no service."""
        binding = db.execute("SELECT * FROM service_bindings WHERE principal = ?", (service,)).fetchone()
        if binding is None:
            raise Unauthenticated("not a service identity")
        return binding

    def _resource(self, db: sqlite3.Connection, resource: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM resources WHERE id = ?", (resource,)).fetchone()
        if row is None:
            raise Invalid("unknown resource or permission")
        return row

    def _approver(
        self,
        db: sqlite3.Connection,
        actor: str,
        resource: sqlite3.Row,
        permissions: set[str],
    ) -> None:
        """Active owner-team member in the approver role holding standing authority for every permission."""
        member = db.execute(
            "SELECT role, authority FROM memberships WHERE principal = ? AND team = ? AND active = 1",
            (actor, resource["owner_team"]),
        ).fetchone()
        standing = set() if member is None else set(json.loads(member["authority"]).get(resource["id"], ()))
        if member is None or member["role"] != resource["approver_role"] or not permissions <= standing:
            raise Forbidden("not an eligible approver")

    def _lifetime(self, resource: sqlite3.Row, until: int | None) -> None:
        now = self._now()
        window = f"send until as a unix time within {resource['max_seconds']} seconds from now"
        if until is None:
            if not resource["allow_ongoing"]:
                raise Invalid(f"ongoing access is not allowed for this resource: {window}")
        elif not now < until <= now + resource["max_seconds"]:
            raise Invalid(f"deadline outside the allowed window: {window}")

    def audit_log(self) -> list[dict[str, Any]]:
        """The IAM audit trail (trusted/operator read; contains no bearer material)."""
        rows = self._db.execute("SELECT at, kind, actor, detail FROM audit ORDER BY id").fetchall()
        return [
            {"at": r["at"], "kind": r["kind"], "actor": r["actor"], **json.loads(r["detail"])} for r in rows
        ]

    def _insert_grants(  # noqa: PLR0913
        self,
        db: sqlite3.Connection,
        principal: str,
        resource: str,
        permissions: Iterable[str],
        expires_at: int | None,
        source: str,
        request: str | None,
        granted_by: str,
    ) -> None:
        for permission in sorted(permissions):
            db.execute(
                "INSERT INTO grants(principal, resource, permission, starts_at, expires_at, source, "
                "request, granted_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (principal, resource, permission, self._now(), expires_at, source, request, granted_by),
            )

    def _generation(self, db: sqlite3.Connection, principal: str, label: str) -> int:
        row = db.execute(
            "SELECT MAX(generation) FROM credentials WHERE principal = ? AND label = ?",
            (principal, label),
        ).fetchone()
        return row[0] or 0

    def _insert_credential(
        self,
        db: sqlite3.Connection,
        principal: str,
        audiences: list[str],
        ttl: int | None,
        generation: int,
        label: str,
        *,
        token: str | None = None,
        now: int | None = None,
    ) -> str:
        token = token or self._token()
        issued = self._now() if now is None else now
        expires = None if ttl is None else issued + ttl
        db.execute(
            "INSERT INTO credentials VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (digest(token), principal, json.dumps(audiences), label, generation, issued, expires),
        )
        return token
