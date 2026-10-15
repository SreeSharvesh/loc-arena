"""The IAM core: principals, credentials, resource registry, requests, grants and online decisions.

``Bootstrap`` methods (``add_*``, ``register_resource``, ``bind_service``, ``issue_credential``,
``rotate_credential``, ``disable_principal``, ``remove_member``) are trusted startup/lifecycle authority,
never exposed to agents. ``Requests`` methods authenticate the caller from a bearer credential; none trusts
a caller-supplied principal. ``Workloads.issue_workload`` lets a resource service create a narrower principal
for work its caller launches.
"""

from __future__ import annotations

import json

from meridian_iam.contracts import (
    Decision,
    Unauthenticated,
)
from meridian_iam.core.base import ACTIVE
from meridian_iam.core.bootstrap import Bootstrap
from meridian_iam.core.lifecycle import Lifecycle
from meridian_iam.core.requests import Requests
from meridian_iam.core.workloads import Workloads


class Iam(Bootstrap, Requests, Lifecycle, Workloads):
    """The whole core: identity and lifecycle, requests and grants, and the online decision."""

    def identify(self, token: str, audience: str) -> str:
        """The principal a credential names for ``audience``, or ``Unauthenticated``."""
        with self._tx() as db:
            return self._auth(db, token, audience)

    def authorize(
        self,
        service_token: str,
        caller_token: str | None,
        operation: str,
        target: str | None = None,
    ) -> Decision:
        """Decide one call: a bad credential raises ``Unauthenticated``; a missing right is a deny."""
        with self._tx() as db:
            service = self._auth(db, service_token, "iam")
            binding = db.execute("SELECT * FROM service_bindings WHERE principal = ?", (service,)).fetchone()
            if binding is None:
                raise Unauthenticated("not a service identity")
            resource = binding["resource"]
            ops = json.loads(
                db.execute("SELECT operations FROM resources WHERE id = ?", (resource,)).fetchone()[0],
            )
            spec = ops.get(operation)
            principal = "anonymous"
            if spec is None:
                allow, reason = False, "unknown_operation"
            elif spec["public"]:
                allow, reason = True, "public"
            else:
                principal = self._auth(db, caller_token, binding["audience"])
                if (
                    spec["target"] and not target
                ):  # for every kind of operation: a call names its object or is refused
                    allow, reason = False, "target_required"
                elif spec.get("authenticated"):
                    allow, reason = True, "authenticated"
                else:
                    held = db.execute(
                        f"SELECT 1 FROM grants WHERE principal = :p AND resource = :r AND permission = :perm "
                        f"AND {ACTIVE}",
                        {"p": principal, "r": resource, "perm": spec["permission"], "now": self._now()},
                    ).fetchone()
                    allow, reason = (True, "granted") if held else (False, "missing_permission")
            decision = Decision(self._id(), principal, resource, operation, target, allow, reason)
            self._audit(
                db,
                "authorize",
                principal,
                allow=allow,
                reason=reason,
                decision=decision.decision_id,
                operation=operation,
                target=target,
            )
            return decision
