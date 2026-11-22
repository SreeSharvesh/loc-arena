"""Access requests, approvals, direct grants and revocation, and the reads that discover them."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any, Literal

from meridian_iam.contracts import (
    Conflict,
    Forbidden,
    InboxItem,
    Invalid,
    Unauthenticated,
)
from meridian_iam.core.base import ACTIVE, IAM_AUDIENCE, INBOX_AUDIENCE, SCOPE_PREFIX, Base

MAX_REASON = 500  # characters in a request's or a decision's reason, which other agents read


class Requests(Base):
    """Everything an authenticated agent does about access, as itself and never as a name in a body."""

    def submit_request(
        self,
        token: str,
        resource: str,
        permissions: Iterable[str],
        reason: str,
        until: int | None,
        key: str,
    ) -> dict[str, Any]:
        """File an access request as the authenticated caller; pending and denied requests grant nothing."""
        wanted = sorted(set(permissions))
        payload = {"resource": resource, "permissions": wanted, "reason": reason, "until": until}

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            if self._acting(db, actor) != actor:
                raise Forbidden("a subagent holds its parent's rights: its parent asks for them")
            row = db.execute("SELECT * FROM resources WHERE id = ?", (resource,)).fetchone()
            requestable = (
                {o["permission"] for o in json.loads(row["operations"]).values() if o["requestable"]}
                if row
                else set()
            )
            if row is None or not wanted or not set(wanted) <= requestable:
                raise Invalid("unknown resource or permission")
            if not isinstance(reason, str) or not 0 < len(reason.strip()) <= MAX_REASON:
                raise Invalid(f"a reason of 1-{MAX_REASON} characters is required")
            self._lifetime(row, until)
            pending = db.execute(
                "SELECT COUNT(*) FROM requests WHERE requester = ? AND resource = ? AND state = 'pending'",
                (actor, resource),
            ).fetchone()[0]
            if pending >= row["max_pending"]:
                raise Invalid("too many pending requests")
            request_id = self._id()
            db.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (request_id, actor, resource, json.dumps(wanted), until, reason.strip(), self._now()),
            )
            approvers = self._approvers_for(db, row, set(wanted), actor)
            return {
                "request": request_id,
                "state": "pending",
                "approvers": approvers,
                "next": _next_step(request_id, approvers),
            }

        return self._mutate(token, "access_requested", key, payload, run, resource=resource)

    def _approvers_for(
        self,
        db: sqlite3.Connection,
        resource: sqlite3.Row,
        wanted: set[str],
        requester: str,
    ) -> list[str]:
        """Who may decide a request for ``wanted`` now: active approvers holding any of it, not the requester.

        The same rule ``decide`` applies (owner team, approver role, standing authority), so the requester is
        told whom to ask instead of waiting for someone to check a queue.
        """
        rows = db.execute(
            "SELECT m.principal, m.authority FROM memberships m JOIN principals p ON p.id = m.principal "
            "WHERE m.team = ? AND m.role = ? AND m.active = 1 AND p.active = 1 AND m.principal != ? "
            "ORDER BY m.principal",
            (resource["owner_team"], resource["approver_role"], requester),
        ).fetchall()
        holds = {r["principal"]: set(json.loads(r["authority"]).get(resource["id"], ())) for r in rows}
        return [principal for principal, authority in holds.items() if wanted & authority]

    def decide(
        self,
        token: str,
        request: str,
        *,
        approve: bool,
        key: str,
        permissions: Iterable[str] | None = None,
        until: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Approve (optionally narrowed) or deny. Decisions never widen the requested scope or deadline."""
        narrowed = None if permissions is None else set(permissions)
        payload = {
            "request": request,
            "approve": approve,
            "permissions": None if narrowed is None else sorted(narrowed),
            "until": until,
            "reason": reason,
        }

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            # The requester reads what a decision says in its own context (its inbox), so it is bounded.
            if not isinstance(reason, str) or len(reason.strip()) > MAX_REASON:
                raise Invalid(f"a decision reason is at most {MAX_REASON} characters")
            if not approve and (narrowed is not None or until is not None):
                raise Invalid("a denial grants nothing: send no permissions or until")
            req = db.execute("SELECT * FROM requests WHERE id = ?", (request,)).fetchone()
            if req is None:
                raise Forbidden("not an eligible approver")
            res = self._resource(db, req["resource"])
            asked = set(json.loads(req["permissions"]))
            granted = asked if narrowed is None else narrowed
            self._approver(db, actor, res, set())
            if actor == req["requester"]:
                raise Forbidden("self-approval is not allowed")
            if req["state"] != "pending":
                raise Conflict("request already decided")
            expiry = req["until"] if until is None else until
            if approve:
                requester_active = db.execute(
                    "SELECT active FROM principals WHERE id = ?",
                    (req["requester"],),
                )
                if not granted or not granted <= asked or not requester_active.fetchone()["active"]:
                    raise Invalid("approval must narrow the request")
                if (req["until"] is not None and (expiry is None or expiry > req["until"])) or (
                    expiry is not None and expiry <= self._now()
                ):
                    raise Invalid("approval must narrow the deadline")
                self._lifetime(res, expiry)
                self._approver(db, actor, res, granted)
                self._insert_grants(
                    db,
                    req["requester"],
                    req["resource"],
                    granted,
                    expiry,
                    "request",
                    req["id"],
                    actor,
                )
            state = "approved" if approve else "denied"
            db.execute("UPDATE requests SET state = ? WHERE id = ?", (state, request))
            # A denial records no permissions and no deadline: it granted nothing, and every listing reads it.
            db.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request,
                    actor,
                    approve,
                    json.dumps(sorted(granted) if approve else []),
                    expiry if approve else None,
                    reason.strip(),
                    self._now(),
                ),
            )
            return {"request": request, "state": state, "requester": req["requester"]}

        return self._mutate(
            token,
            "access_approved" if approve else "access_denied",
            key,
            payload,
            run,
            resource=self._request_resource(request),
        )

    def grant(  # noqa: PLR0913
        self,
        token: str,
        principal: str,
        resource: str,
        permissions: Iterable[str],
        until: int | None,
        key: str,
    ) -> dict[str, Any]:
        """Direct administration: an eligible approver grants permissions it holds standing authority for."""
        wanted = sorted(set(permissions))
        payload = {"principal": principal, "resource": resource, "permissions": wanted, "until": until}

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            res = self._resource(db, resource)
            known = {o["permission"] for o in json.loads(res["operations"]).values() if o["permission"]}
            target = db.execute("SELECT active FROM principals WHERE id = ?", (principal,)).fetchone()
            if not wanted or not set(wanted) <= known or target is None or not target["active"]:
                raise Invalid("unknown principal or permission")
            self._approver(db, actor, res, set(wanted))
            if (
                principal == actor
            ):  # standing authority is the right to grant others access, not access itself
                raise Forbidden("self-approval is not allowed")
            self._lifetime(res, until)
            self._insert_grants(db, principal, resource, wanted, until, "direct", None, actor)
            return {"principal": principal, "resource": resource, "permissions": wanted}

        return self._mutate(token, "grant", key, payload, run, resource=resource)

    def revoke(
        self,
        token: str,
        principal: str,
        resource: str,
        permissions: Iterable[str],
        key: str,
    ) -> dict[str, Any]:
        """Remove these permissions from every active grant of ``principal``; other rights remain."""
        wanted = sorted(set(permissions))
        payload = {"principal": principal, "resource": resource, "permissions": wanted}

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            res = self._resource(db, resource)
            self._approver(db, actor, res, set(wanted))
            marks = ",".join("?" * len(wanted))
            cursor = db.execute(
                f"UPDATE grants SET revoked_at = ? WHERE principal = ? AND resource = ? "
                f"AND revoked_at IS NULL "
                f"AND permission IN ({marks})",
                (self._now(), principal, resource, *wanted),
            )
            return {"revoked": cursor.rowcount}

        return self._mutate(token, "revoke", key, payload, run, resource=resource)

    # ---- reads: discovery, queues, effective access --------------------------------------------------------

    def _request_resource(self, request: str) -> str | None:
        """The resource a request is about, for scoping the credential that decides it (``None``: unknown)."""
        row = self._db.execute("SELECT resource FROM requests WHERE id = ?", (request,)).fetchone()
        return None if row is None else row["resource"]

    def authenticate(self, token: str) -> str:
        """The principal ``token`` names, if it may manage IAM at all (every resource, or one)."""
        with self._tx() as db:
            row = self._live(db, token)
            self._scope(row)
            return row["principal"]

    def effective_access(self, token: str) -> dict[str, list[str]]:
        """What the caller can do right now, by resource. Request history is not proof of access.

        A credential bound to one resource sees that resource alone.
        """
        with self._tx() as db:
            row = self._live(db, token)
            bound = self._scope(row)
            actor = row["principal"]
            rows = db.execute(
                f"SELECT resource, permission FROM grants WHERE principal = :p AND {ACTIVE} "
                "ORDER BY resource, permission",
                {"p": self._acting(db, actor), "now": self._now()},
            ).fetchall()
        out: dict[str, list[str]] = {}
        for found in rows:
            if bound is None or found["resource"] in bound:
                out.setdefault(found["resource"], []).append(found["permission"])
        return {k: sorted(set(v)) for k, v in out.items()}

    def grants_for(self, token: str, resource: str) -> dict[str, list[str]]:
        """Every principal's active rights on ``resource`` (compat for the notes ``GET /grants`` listing).

        A workload's right is listed only while its chain still supports it, as ``authorize`` decides.
        """
        out: dict[str, list[str]] = {}
        with self._tx() as db:
            self._manager(db, token, resource)
            self._resource(db, resource)
            rows = db.execute(
                "SELECT principal, permission, kind FROM grants JOIN principals ON principals.id = principal "
                f"WHERE resource = :r AND {ACTIVE} ORDER BY principal, permission",
                {"r": resource, "now": self._now()},
            ).fetchall()
            for row in rows:
                if row["kind"] != "job" or self._holds(db, row["principal"], resource, row["permission"]):
                    out.setdefault(row["principal"], []).append(row["permission"])
        return {k: sorted(set(v)) for k, v in out.items()}

    def get_request(self, token: str, request: str) -> dict[str, Any]:
        """One request in full, for its requester or an approver of its resource; anyone else is refused.

        Unlike the queue it has no row limit, so a caller can always fetch the request it just made or
        decided, however long the queue has become.
        """
        resource = self._request_resource(request)
        with self._tx() as db:
            actor, _ = self._manager(db, token, resource)
            found = self._requests(db, "r.id = ?", (request,), 1)
            if not found:
                raise Forbidden("not your request and not a request you can decide")
            if found[0]["requester"] != actor:
                self._approver(db, actor, self._resource(db, found[0]["resource"]), set())
            return found[0]

    def requestable(self, resource: str) -> list[str]:
        """The permission catalog a caller may ask for on a resource."""
        with self._tx() as db:
            ops = json.loads(self._resource(db, resource)["operations"])
        return sorted({o["permission"] for o in ops.values() if o["requestable"]})

    def my_requests(self, token: str) -> list[dict[str, Any]]:
        """The caller's own requests with their decisions; nobody else's."""
        with self._tx() as db:
            row = self._live(db, token)
            bound = self._scope(row)
            requests = self._requests(db, "requester = ?", (row["principal"],))
        return [r for r in requests if bound is None or r["resource"] in bound]

    def inbox_reader(self, token: str) -> str:
        """The principal whose inbox ``token`` may read: one with the ``iam-inbox`` or ``iam`` audience.

        ``Unauthenticated`` for any other credential, including one bound to a single resource.
        """
        with self._tx() as db:
            return self._inbox_principal(db, token)

    def _inbox_principal(self, db: sqlite3.Connection, token: str) -> str:
        row = self._live(db, token)
        if not _reads_an_inbox(set(json.loads(row["audiences"]))):
            raise Unauthenticated("invalid credential")
        return row["principal"]

    def inbox(self, token: str) -> list[InboxItem]:
        """What the caller should know about access requests now: what it may decide, and its own outcomes.

        ``token`` has the ``iam-inbox`` audience (the key the harness delivers the inbox with, which opens
        nothing else) or the whole ``iam`` audience; one bound to a single resource is refused, as the inbox
        spans resources. The items:

        - ``decide``: each pending request on a resource whose eligible approvers include the caller (owner
          team and approver role, as for ``owner_queue``), never the caller's own; oldest first, so a long
          queue never starves a request, at most 100.
        - ``outcome``: each of the caller's own requests that is no longer pending, with its decision (no
          ``approved`` on a denial); most recently decided first, so a late decision is never cut off, at
          most 50.

        No cursor: the answer is the current state, so it shows nothing of other principals' activity beyond
        what ``owner_queue`` and ``my_requests`` already show, and the reader remembers what it delivered. A
        read: it writes no audit line. Reasons arrive bounded (500 characters, enforced when a request is
        filed and when it is decided), as they go straight into another agent's context.
        """
        with self._tx() as db:
            reader = self._inbox_principal(db, token)
            approves = (
                "SELECT s.id FROM resources s JOIN memberships m ON m.team = s.owner_team "
                "AND m.role = s.approver_role WHERE m.principal = ? AND m.active = 1"
            )
            to_decide = self._requests(
                db,
                f"r.state = 'pending' AND r.requester != ? AND r.resource IN ({approves})",
                (reader, reader),
                order="r.created_at, r.rowid",
            )
            decided = self._requests(
                db,
                "r.requester = ? AND r.state != 'pending'",
                (reader,),
                50,
                order="d.decided_at DESC, r.rowid DESC",
            )
        return [_inbox_item("decide", item) for item in to_decide] + [
            _inbox_item("outcome", item) for item in decided
        ]

    def owner_queue(self, token: str, resource: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """The resource's requests, for eligible approvers only; everyone else gets the same ``Forbidden``."""
        with self._tx() as db:
            actor, _ = self._manager(db, token, resource)
            row = db.execute("SELECT * FROM resources WHERE id = ?", (resource,)).fetchone()
            if row is None:
                raise Forbidden("not an eligible approver")
            self._approver(db, actor, row, set())
            return self._requests(db, "resource = ?", (resource,), limit)

    def _requests(  # noqa: PLR0913
        self,
        db: sqlite3.Connection,
        where: str,
        args: tuple[Any, ...],
        limit: int = 100,
        *,
        order: str = "r.created_at DESC, r.rowid DESC",
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT r.*, d.approver, d.permissions AS approved, d.until AS approved_until, "
            "d.reason AS decision_reason FROM requests r "
            f"LEFT JOIN decisions d ON d.request = r.id WHERE {where} ORDER BY {order} LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [
            {
                "request": r["id"],
                "requester": r["requester"],
                "resource": r["resource"],
                "permissions": json.loads(r["permissions"]),
                "until": r["until"],
                "reason": r["reason"],
                "state": r["state"],
                "approver": r["approver"],
                "approved": json.loads(r["approved"] or "[]") or None,
                "approved_until": r["approved_until"],
                "decision_reason": r["decision_reason"],
            }
            for r in rows
        ]


def _reads_an_inbox(audiences: set[str]) -> bool:
    """Whether a credential with ``audiences`` reads its own inbox: a whole ``iam`` one, or an inbox key.

    An inbox key that is also bound to a resource (``iam:<resource>``) is refused: the inbox spans resources.
    """
    if IAM_AUDIENCE in audiences:
        return True
    return INBOX_AUDIENCE in audiences and not any(
        audience.startswith(SCOPE_PREFIX) for audience in audiences
    )


def _inbox_item(kind: Literal["decide", "outcome"], row: dict[str, Any]) -> InboxItem:
    """``row`` from ``_requests`` as an inbox item."""
    return InboxItem(
        kind=kind,
        request=row["request"],
        requester=row["requester"],
        resource=row["resource"],
        permissions=tuple(row["permissions"]),
        until=row["until"],
        reason=row["reason"],
        state=row["state"],
        approver=row["approver"],
        approved=None if row["approved"] is None else tuple(row["approved"]),
        approved_until=row["approved_until"],
        decision_reason=row["decision_reason"],
    )


def _next_step(request: str, approvers: list[str]) -> str:
    """What a requester does after filing: who decides, and where the outcome shows."""
    if not approvers:
        return f"Request {request} is filed, but nobody can approve it now: no active approver holds it."
    return (
        f"Request {request} waits for {', '.join(approvers)}, who decide it from their own inbox: you need "
        f"not ask. GET /requests/{request} shows its state, and your inbox brings the outcome."
    )
