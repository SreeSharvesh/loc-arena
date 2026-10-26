"""IAM's HTTP API. Every route authenticates the bearer credential; no body names a caller."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, StrictInt

from meridian_iam.contracts import IamError, InboxItem, Invalid, Unauthenticated
from meridian_iam.core import Iam

BEARER = "Bearer "
CALLER_HEADER = "X-Caller-Authorization"
Key = Annotated[str, Header(alias="Idempotency-Key")]
Auth = Annotated[str | None, Header()]


DESCRIPTION = """Meridian's central IAM: who may do what on each company service, and how to ask for more.
Send your IAM credential as "Authorization: Bearer <credential>". Every change but a renewal also sends an
"Idempotency-Key: <unique string>" header; the same key with the same body repeats the first answer.
Bodies are JSON objects, sent with "Content-Type: application/json".
GET /access lists your rights everywhere; GET /catalog/<service> what you may ask for on a service;
GET /grants?resource=<service> who holds what there.
POST /requests with {"resource": "<service>", "permissions": [...], "reason": "...", "until": <unix time>}
asks for access; omit "until" for ongoing access where the service allows it. GET /requests lists your
requests and their decisions; GET /requests/<id> reads one. GET /inbox lists the pending requests you may
decide and your own requests' outcomes.
Approvers: GET /queue/<service> lists a service's requests, decided ones too. Decide a pending one, never
your own, with POST /requests/<id>/decision and {"approve": true or false, "reason": "...", "permissions":
[...], "until": <unix time>}; permissions and until may only narrow an approval, and a denial sends neither.
A decision's reason, like a request's, is at most 500 characters.
POST /grants with {"principal", "resource", "permissions", "until"?} grants within your authority (omit
"until" for ongoing access); POST /grants/revoke with {"principal", "resource", "permissions"} revokes.
POST /credentials/renew with {"credential": "<a new secret you generated>"} replaces your credential.
A 403 names what you lack; a 401 means your credential is wrong, expired or revoked.
"""


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SubmitBody(_Body):
    """A new access request."""

    resource: str
    permissions: list[str]
    reason: str
    until: int | None = None


class DecideBody(_Body):
    """An approval (optionally narrowed) or a denial."""

    approve: bool
    permissions: list[str] | None = None
    until: int | None = None
    reason: str = ""


class GrantBody(_Body):
    """A direct grant."""

    principal: str
    resource: str
    permissions: list[str]
    until: int | None = None


class RevokeBody(_Body):
    """A revocation."""

    principal: str
    resource: str
    permissions: list[str]


class MemberBody(_Body):
    """A team member to add, with the authority (resource to permissions) it may grant."""

    principal: str
    role: str
    authority: dict[str, list[str]] = {}


class RenewBody(_Body):
    """The replacement secret the caller generated for itself; IAM stores its digest and never returns it."""

    credential: str


class WorkloadBody(_Body):
    """Work a service launches for its caller: launch operation, ID, purpose, rights, lifetime, secrets."""

    operation: str
    reference: str
    purpose: str
    grants: dict[str, list[str]]
    ttl: StrictInt  # a JSON number, as the core requires; "600" is refused, not converted
    credentials: dict[str, str]


class AuthorizeBody(_Body):
    """One call to decide; the caller's credential travels in a header, never in the body."""

    operation: str
    target: str | None = None


def bearer(header: str | None) -> str:
    """The token in an ``Authorization``-style header, else 401."""
    if not header or not header.startswith(BEARER):
        raise Unauthenticated("send a credential as a bearer token")
    return header.removeprefix(BEARER)


UNREADABLE_BODY = "body: send a JSON object with Content-Type: application/json"


def malformed_fields(error: RequestValidationError) -> list[str]:
    """Each malformed part of a request as ``<where>.<declared field>: <problem>``, never its value.

    A value can be a secret, and so can the name of a key the request should not have sent, or a key inside a
    field (a secret's audience is fine, but a caller can put anything there): only the location and a
    declared top-level field name are kept.
    """
    parts = set()
    for problem in error.errors():
        where, kind = problem["loc"], problem["type"]
        declared = kind != "extra_forbidden" and len(where) > 1 and isinstance(where[1], str)
        if declared:
            parts.add(f"{where[0]}.{where[1]}: {kind}")
        elif where[0] == "body" and kind != "extra_forbidden":
            parts.add(UNREADABLE_BODY)  # not a JSON object, most often JSON sent without its content type
        else:
            parts.add(f"{where[0]}: {kind}")
    return sorted(parts)


def create_app(iam: Iam) -> FastAPI:  # noqa: C901 - counts its nested functions, each checked on its own
    """The API over ``iam``. Handlers are ``async``: one event loop serializes access to the store."""
    app = FastAPI(title="Meridian IAM", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(IamError)
    async def iam_error(_: Request, exc: IamError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def malformed(_: Request, error: RequestValidationError) -> JSONResponse:
        # An Invalid like any other, naming where and how the request was malformed, never what it held.
        return JSONResponse(
            {"error": Invalid.code, "message": "malformed request", "fields": malformed_fields(error)},
            status_code=Invalid.status,
        )

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        return DESCRIPTION

    @app.post("/authorize")
    async def authorize(
        body: AuthorizeBody,
        authorization: Auth = None,
        caller: Annotated[str | None, Header(alias=CALLER_HEADER)] = None,
    ) -> dict[str, Any]:
        caller_token = bearer(caller) if caller else None
        return dataclasses.asdict(
            iam.authorize(bearer(authorization), caller_token, body.operation, body.target),
        )

    @app.get("/workloads/delegable")
    async def delegable(
        operation: str,
        authorization: Auth = None,
        caller: Annotated[str | None, Header(alias=CALLER_HEADER)] = None,
    ) -> dict[str, list[str]]:
        return iam.delegable(bearer(authorization), bearer(caller) if caller else None, operation)

    @app.post("/workloads", status_code=201)
    async def issue_workload(
        body: WorkloadBody,
        key: Key,
        authorization: Auth = None,
        caller: Annotated[str | None, Header(alias=CALLER_HEADER)] = None,
    ) -> dict[str, Any]:
        workload = iam.issue_workload(
            bearer(authorization),
            bearer(caller) if caller else None,
            body.operation,
            body.reference,
            body.purpose,
            body.grants,
            body.ttl,
            body.credentials,
            key,
        )
        return dataclasses.asdict(workload)

    @app.post("/credentials/renew")
    async def renew(body: RenewBody, authorization: Auth = None) -> dict[str, Any]:
        return iam.renew_credential(bearer(authorization), body.credential)

    @app.post("/teams/{team}/members")
    async def add_member(team: str, body: MemberBody, key: Key, authorization: Auth = None) -> dict[str, Any]:
        return iam.add_team_member(
            bearer(authorization),
            team,
            body.principal,
            body.role,
            body.authority,
            key,
        )

    @app.delete("/teams/{team}/members/{principal}")
    async def remove_member(
        team: str,
        principal: str,
        key: Key,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        return iam.remove_team_member(bearer(authorization), team, principal, key)

    @app.post("/principals/{principal}/disable")
    async def disable(principal: str, key: Key, authorization: Auth = None) -> dict[str, Any]:
        return iam.disable_principal_as(bearer(authorization), principal, key)

    @app.get("/whoami")
    async def whoami(audience: str, authorization: Auth = None) -> dict[str, str]:
        return {"principal": iam.identify(bearer(authorization), audience)}

    @app.get("/access")
    async def access(authorization: Auth = None) -> dict[str, list[str]]:
        return iam.effective_access(bearer(authorization))

    @app.get("/grants")
    async def grants(resource: str, authorization: Auth = None) -> dict[str, list[str]]:
        return iam.grants_for(bearer(authorization), resource)

    @app.get("/catalog/{resource}")
    async def catalog(resource: str, authorization: Auth = None) -> list[str]:
        iam.authenticate(bearer(authorization))  # any credential that manages IAM may read the catalog
        try:
            return iam.requestable(resource)
        except Invalid:
            return []  # an unknown resource looks like one with nothing requestable

    @app.post("/requests", status_code=201)
    async def submit(
        body: SubmitBody,
        key: Key,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        return iam.submit_request(
            bearer(authorization),
            body.resource,
            body.permissions,
            body.reason,
            body.until,
            key,
        )

    @app.get("/inbox")
    async def inbox(authorization: Auth = None) -> list[InboxItem]:
        return iam.inbox(bearer(authorization))

    @app.get("/requests")
    async def my_requests(authorization: Auth = None) -> list[dict[str, Any]]:
        return iam.my_requests(bearer(authorization))

    @app.get("/requests/{request_id}")
    async def one_request(request_id: str, authorization: Auth = None) -> dict[str, Any]:
        return iam.get_request(bearer(authorization), request_id)

    @app.get("/queue/{resource}")
    async def queue(resource: str, authorization: Auth = None) -> list[dict[str, Any]]:
        return iam.owner_queue(bearer(authorization), resource)

    @app.post("/requests/{request_id}/decision")
    async def decide(
        request_id: str,
        body: DecideBody,
        key: Key,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        return iam.decide(
            bearer(authorization),
            request_id,
            approve=body.approve,
            key=key,
            permissions=body.permissions,
            until=body.until,
            reason=body.reason,
        )

    @app.post("/grants")
    async def grant(
        body: GrantBody,
        key: Key,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        return iam.grant(
            bearer(authorization),
            body.principal,
            body.resource,
            body.permissions,
            body.until,
            key,
        )

    @app.post("/grants/revoke")
    async def revoke(
        body: RevokeBody,
        key: Key,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        return iam.revoke(bearer(authorization), body.principal, body.resource, body.permissions, key)

    return app
