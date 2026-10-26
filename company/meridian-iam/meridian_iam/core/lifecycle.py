"""Lifecycle an agent does through IAM itself: renew a credential, change a team, disable a principal.

None of this needs bootstrap authority. Renewal hands the replacement to the holder of the old credential and
nobody else; a team's approvers add and remove its members within the authority they hold; only an operator
disables a principal.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from meridian_iam.contracts import Conflict, Forbidden, Invalid
from meridian_iam.core.base import OPERATOR_ROLE, OPERATOR_TEAM, Base, digest

__all__ = ["OPERATOR_ROLE", "OPERATOR_TEAM", "Lifecycle"]

REPLACEMENT = re.compile(r"[A-Za-z0-9_-]{32,128}")


class Lifecycle(Base):
    """Credential renewal and the administration of teams and principals, all as an authenticated agent."""

    def renew_credential(self, token: str, replacement: str) -> dict[str, Any]:
        """Replace the caller's own credential with ``replacement``, a secret the caller generated itself.

        IAM never invents or returns the secret, so a lost reply loses nothing: the holder already has it.
        The old credential keeps working for the grace window (bounded overlap) and then lapses. During that
        window it may repeat this exact renewal, which is idempotent, but can never name a different
        replacement: once renewed, a credential has no authority to replace the newer one. Audience and
        lifetime are the old credential's, so a renewal widens neither, and a disabled principal cannot renew.
        """
        if not isinstance(replacement, str) or not REPLACEMENT.fullmatch(replacement):
            raise Invalid("the replacement must be a secret of 32-128 URL-safe characters")
        with self._tx() as db:
            row = self._live(db, token)
            self._scope(row)  # any credential that manages IAM, whole or for one resource, may renew itself
            fresh = digest(replacement)
            if row["superseded_by"] is not None:
                if row["superseded_by"] != fresh:
                    raise Conflict("this credential was already renewed: use its replacement")
                renewed = db.execute(
                    "SELECT generation FROM credentials WHERE digest = ?",
                    (fresh,),
                ).fetchone()
                return {"generation": renewed["generation"], "previous_valid_until": row["expires_at"]}
            if db.execute("SELECT 1 FROM credentials WHERE digest = ?", (fresh,)).fetchone():
                raise Conflict("that secret is already in use")
            now = self._now()
            lapses = now + self.renewal_grace_seconds
            lapses = lapses if row["expires_at"] is None else min(lapses, row["expires_at"])
            db.execute(
                "UPDATE credentials SET expires_at = ?, superseded_by = ? WHERE digest = ?",
                (lapses, fresh, digest(token)),
            )
            lifetime = None if row["expires_at"] is None else row["expires_at"] - row["issued_at"]
            generation = self._generation(db, row["principal"], row["label"]) + 1
            self._insert_credential(
                db,
                row["principal"],
                json.loads(row["audiences"]),
                lifetime,
                generation,
                row["label"],
                token=replacement,
            )
            self._audit(db, "credential_renewed", row["principal"], label=row["label"], generation=generation)
            return {"generation": generation, "previous_valid_until": lapses}

    def _team_resources(self, db: sqlite3.Connection, team: str) -> dict[str, sqlite3.Row]:
        rows = db.execute("SELECT * FROM resources WHERE owner_team = ?", (team,)).fetchall()
        return {row["id"]: row for row in rows}

    def _team_admin(
        self,
        db: sqlite3.Connection,
        actor: str,
        team: str,
        resources: Mapping[str, sqlite3.Row],
    ) -> None:
        """Raise ``Forbidden`` unless ``actor`` is an active approver-role member of ``team``."""
        roles = {row["approver_role"] for row in resources.values()}
        member = db.execute(
            "SELECT role FROM memberships WHERE principal = ? AND team = ? AND active = 1",
            (actor, team),
        ).fetchone()
        if member is None or member["role"] not in roles:
            raise Forbidden("not a team administrator")

    def add_team_member(  # noqa: PLR0913, C901 - C901 counts its nested functions, each checked on its own
        self,
        token: str,
        team: str,
        principal: str,
        role: str,
        authority: Mapping[str, Iterable[str]],
        key: str,
    ) -> dict[str, Any]:
        """Add ``principal`` to ``team``, or widen its authority there (resource to permissions).

        The caller must itself be an approver of the team holding every permission it hands on, so authority
        is delegated and never created. It is additive: nothing here narrows or removes a member's authority,
        so a weaker owner cannot strip a stronger one (narrowing is ``remove_team_member``, which needs cover
        for all it removes). Authority needs its resource's approver role, and the operator team is reserved.
        """
        wanted = {resource: sorted(set(perms)) for resource, perms in authority.items()}
        payload = {"team": team, "principal": principal, "role": role, "authority": wanted}

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            if team == OPERATOR_TEAM:
                raise Forbidden("operator membership is reserved")
            resources = self._team_resources(db, team)
            if not resources:
                raise Forbidden("not a team administrator")
            self._team_admin(db, actor, team, resources)
            if not set(wanted) <= set(resources) or any(not perms for perms in wanted.values()):
                raise Invalid("authority must name permissions on a resource the team owns")
            for resource, perms in wanted.items():
                if role != resources[resource]["approver_role"]:
                    raise Invalid("authority needs the approver role of its resource")
                self._approver(db, actor, resources[resource], set(perms))
            target = db.execute("SELECT active FROM principals WHERE id = ?", (principal,)).fetchone()
            if target is None or not target["active"]:
                raise Invalid("unknown or inactive principal")
            existing = db.execute(
                "SELECT role, authority, active FROM memberships WHERE principal = ? AND team = ?",
                (principal, team),
            ).fetchone()
            merged: dict[str, list[str]] = {}
            if existing is not None and existing["active"]:
                if existing["role"] != role:
                    raise Invalid("the member already has another role in this team")
                merged = json.loads(existing["authority"])
            for resource, perms in wanted.items():
                merged[resource] = sorted(set(merged.get(resource, ())) | set(perms))
            db.execute(
                "INSERT OR REPLACE INTO memberships VALUES (?, ?, ?, ?, 1, ?)",
                (principal, team, role, json.dumps(merged), f"added by {actor}"),
            )
            return {"team": team, "principal": principal, "role": role, "authority": merged}

        return self._mutate(token, "member_added", key, payload, run)

    def remove_team_member(self, token: str, team: str, principal: str, key: str) -> dict[str, Any]:
        """End ``principal``'s membership of ``team``: its leaving, or an approver covering its authority.

        Grants already issued stay (they are independent); the member's standing authority ends at once.
        """
        payload = {"team": team, "principal": principal}

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            if team == OPERATOR_TEAM:
                raise Forbidden("operator membership is reserved")
            member = db.execute(
                "SELECT authority FROM memberships WHERE principal = ? AND team = ? AND active = 1",
                (principal, team),
            ).fetchone()
            if member is None:
                raise Invalid("no such member")
            if actor != principal:
                resources = self._team_resources(db, team)
                self._team_admin(db, actor, team, resources)
                for resource, perms in json.loads(member["authority"]).items():
                    if resource in resources:
                        self._approver(db, actor, resources[resource], set(perms))
            db.execute(
                "UPDATE memberships SET active = 0, provenance = ? WHERE principal = ? AND team = ?",
                (f"removed by {actor}", principal, team),
            )
            return {"team": team, "principal": principal, "active": False}

        return self._mutate(token, "member_removed", key, payload, run)

    def disable_principal_as(self, token: str, principal: str, key: str) -> dict[str, Any]:
        """Disable ``principal`` (all its credentials fail): only an active operator may, and never itself."""
        payload = {"principal": principal}

        def run(db: sqlite3.Connection, actor: str) -> dict[str, Any]:
            operator = db.execute(
                "SELECT 1 FROM memberships WHERE principal = ? AND team = ? AND role = ? AND active = 1",
                (actor, OPERATOR_TEAM, OPERATOR_ROLE),
            ).fetchone()
            if operator is None:
                raise Forbidden("not an operator")
            if principal == actor:
                raise Invalid("an operator cannot disable itself")
            if db.execute("UPDATE principals SET active = 0 WHERE id = ?", (principal,)).rowcount == 0:
                raise Invalid("unknown principal")
            return {"principal": principal, "active": False}

        return self._mutate(token, "principal_disabled", key, payload, run)
