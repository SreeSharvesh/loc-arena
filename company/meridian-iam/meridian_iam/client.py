"""A stdlib-only IAM client: a resource service's image needs no extra dependency.

Reads and the authorization check retry on transport failure; mutations never do. A transport failure, 5xx or
malformed answer becomes ``Unavailable``: the caller executes nothing and does not report a missing grant.
Token-bearing headers and transport exception text are never put in errors.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from typing import Any

from meridian_iam.contracts import (
    Conflict,
    Decision,
    Forbidden,
    IamError,
    Invalid,
    Unauthenticated,
    Unavailable,
    Workload,
)

_BY_CODE: dict[str, type[IamError]] = {
    cls.code: cls for cls in (Unauthenticated, Forbidden, Invalid, Conflict, Unavailable)
}


class IamClient:
    """Talks to one IAM service as one service identity (``token``)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 2.0, retries: int = 2) -> None:
        """``retries`` bounds extra attempts for safe calls only."""
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._retries = retries

    def _call(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        safe: bool = False,
    ) -> Any:  # noqa: ANN401 - decoded JSON
        request_headers = {
            "Authorization": f"Bearer {token or self._token}",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        data = None if body is None else json.dumps(body).encode()
        last: Unavailable | None = None
        for _ in range(1 + (self._retries if safe else 0)):
            request = urllib.request.Request(  # noqa: S310 - the base URL is trusted configuration
                self._base + path,
                data=data,
                headers=request_headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code >= 500:
                    last = Unavailable("IAM unavailable")
                    continue
                raise self._error(exc) from None
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                last = Unavailable("IAM unavailable")
        raise last or Unavailable("IAM unavailable")

    @staticmethod
    def _error(exc: urllib.error.HTTPError) -> IamError:
        try:
            payload = json.loads(exc.read())
            return _BY_CODE[payload["error"]](str(payload.get("message", "")))
        except (ValueError, KeyError, TypeError):
            return Unavailable("malformed IAM answer")

    def authorize(self, caller_token: str | None, operation: str, target: str | None = None) -> Decision:
        """One online decision for one call, as this client's service identity."""
        headers = {"X-Caller-Authorization": f"Bearer {caller_token}"} if caller_token else {}
        raw = self._call(
            "POST",
            "/authorize",
            headers=headers,
            body={"operation": operation, "target": target},
            safe=True,
        )
        try:
            fields = {
                k: raw[k]
                for k in ("decision_id", "principal", "resource", "operation", "target", "allow", "reason")
            }
            valid = (
                all(
                    isinstance(fields[k], str)
                    for k in ("decision_id", "principal", "resource", "operation", "reason")
                )
                and isinstance(fields["allow"], bool)
                and (fields["target"] is None or isinstance(fields["target"], str))
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise Unavailable("malformed IAM decision")
        return Decision(**fields)

    def issue_workload(  # noqa: PLR0913
        self,
        caller_token: str | None,
        operation: str,
        reference: str,
        purpose: str,
        grants: dict[str, list[str]],
        ttl: int,
        credential: str,
        key: str,
    ) -> Workload:
        """Issue a workload identity as this client's service identity, for the caller launching the work.

        A mutation, so never retried. ``credential`` is the secret this service generated for the workload and
        keeps to hand over; a retry after a lost reply sends the same secret under the same ``key``. A
        malformed answer is ``Unavailable``.
        """
        raise NotImplementedError

    # Management calls forward the caller's own credential; the service identity is not used.

    def renew_credential(self, token: str) -> dict[str, Any]:
        """Replace this credential with a secret generated here; the reply never carries it.

        The returned dict holds ``credential`` (the new secret, known only to this caller), the new
        ``generation`` and when the old credential lapses. A repeat after a lost reply needs the same secret,
        so a caller that wants recovery keeps its replacement before it sends.
        """
        replacement = "mi_" + secrets.token_urlsafe(32)
        return self.renew_with(token, replacement)

    def renew_with(self, token: str, replacement: str) -> dict[str, Any]:
        """Renew to the given ``replacement`` secret (idempotent for the same one while the old is valid)."""
        reply = self._call("POST", "/credentials/renew", token=token, body={"credential": replacement})
        return {**reply, "credential": replacement}

    def add_team_member(self, token: str, team: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        """Add a member to a team (an approver of it, delegating authority it holds)."""
        return self._call(
            "POST",
            f"/teams/{team}/members",
            token=token,
            headers={"Idempotency-Key": key},
            body=body,
        )

    def remove_team_member(self, token: str, team: str, principal: str, key: str) -> dict[str, Any]:
        """Remove a member from a team: one's own leaving, or an approver's."""
        return self._call(
            "DELETE",
            f"/teams/{team}/members/{principal}",
            token=token,
            headers={"Idempotency-Key": key},
        )

    def disable_principal(self, token: str, principal: str, key: str) -> dict[str, Any]:
        """Disable a principal (an operator's)."""
        return self._call(
            "POST",
            f"/principals/{principal}/disable",
            token=token,
            headers={"Idempotency-Key": key},
        )

    def identify(self, token: str, audience: str) -> str:
        """The principal ``token`` names for ``audience`` (``Unauthenticated`` if it names none)."""
        reply = self._call("GET", f"/whoami?audience={audience}", token=token, safe=True)
        principal = reply.get("principal") if isinstance(reply, dict) else None
        if not isinstance(principal, str):
            raise Unavailable("malformed IAM answer")
        return principal

    def effective_access(self, token: str) -> dict[str, list[str]]:
        """What the credential can do now."""
        return self._call("GET", "/access", token=token, safe=True)

    def grants_for(self, token: str, resource: str) -> dict[str, list[str]]:
        """Every principal's active rights on a resource."""
        return self._call("GET", f"/grants?resource={resource}", token=token, safe=True)

    def catalog(self, token: str, resource: str) -> list[str]:
        """The permissions requestable on ``resource``."""
        return self._call("GET", f"/catalog/{resource}", token=token, safe=True)

    def submit_request(self, token: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        """File an access request as the credential's principal."""
        return self._call("POST", "/requests", token=token, headers={"Idempotency-Key": key}, body=body)

    def my_requests(self, token: str) -> list[dict[str, Any]]:
        """The credential's own requests."""
        return self._call("GET", "/requests", token=token, safe=True)

    def get_request(self, token: str, request: str) -> dict[str, Any]:
        """One request in full (not bounded by the queue limit), for its requester or an approver of it."""
        return self._call("GET", f"/requests/{request}", token=token, safe=True)

    def owner_queue(self, token: str, resource: str) -> list[dict[str, Any]]:
        """A resource's queue, for eligible approvers."""
        return self._call("GET", f"/queue/{resource}", token=token, safe=True)

    def decide(self, token: str, request: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        """Approve or deny a request."""
        return self._call(
            "POST",
            f"/requests/{request}/decision",
            token=token,
            headers={"Idempotency-Key": key},
            body=body,
        )

    def grant(self, token: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        """Direct administration: grant."""
        return self._call("POST", "/grants", token=token, headers={"Idempotency-Key": key}, body=body)

    def revoke(self, token: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        """Direct administration: revoke."""
        return self._call("POST", "/grants/revoke", token=token, headers={"Idempotency-Key": key}, body=body)
