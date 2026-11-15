"""A resource service guarded by a real IAM server: request, approval, retry, outage, coverage, mismatch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from iam_server import serve
from iam_support import NOTES, wait_until_refused

from meridian_iam.client import IamClient
from meridian_iam.contracts import Decision, ManifestError, Unavailable, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.integration.asgi import dependency, verify_app
from meridian_iam.service import create_app

MANIFEST = {
    **NOTES,
    "http": {**NOTES["http"], "GET /openapi.json": "health"},
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def stack(iam: Iam, tokens: dict[str, str]) -> Iterator[dict[str, Any]]:
    """IAM server + a notes-like resource app whose handlers record every execution."""
    executed: list[str] = []
    with serve(iam) as (url, server):
        guard = Guard(load_manifest(MANIFEST), IamClient(url, tokens["svc"], timeout=1, retries=1))
        app = FastAPI(docs_url=None, redoc_url=None, dependencies=[Depends(dependency(guard))])

        @app.get("/health")
        async def health() -> str:
            return "ok"

        @app.get("/notes/{key}")
        async def read_note(key: str) -> str:
            executed.append(f"read {key}")
            return "text"

        @app.put("/notes/{key}")
        async def write_note(key: str) -> str:
            executed.append(f"write {key}")
            return "ok"

        @app.post("/admin")
        async def admin() -> str:
            executed.append("admin")
            return "ok"

        verify_app(guard, app)
        yield {
            "client": TestClient(app),
            "executed": executed,
            "url": url,
            "server": server,
            "guard": guard,
            "app": app,
        }


def test_request_approve_retry_flow(stack: dict[str, Any], tokens: dict[str, str]) -> None:
    resource, executed = stack["client"], stack["executed"]
    assert resource.get("/health").status_code == 200  # public, no credential
    assert resource.put("/notes/a").status_code == 401  # no credential
    assert resource.put("/notes/a", headers=bearer("junk")).status_code == 401

    denied = resource.put("/notes/a", headers=bearer(tokens["alice"]))
    assert denied.status_code == 403
    assert denied.json()["detail"] == {
        "error": "forbidden",
        "resource": "notes",
        "permission": "write",
        "request_with": "POST /requests on the IAM service",
    }
    assert executed == []

    alice, lead = IamClient(stack["url"], tokens["alice"]), IamClient(stack["url"], tokens["lead"])
    request = alice.submit_request(
        tokens["alice"],
        {"resource": "notes", "permissions": ["write"], "reason": "r", "until": 2000},
        "k",
    )
    assert (
        resource.put("/notes/a", headers=bearer(tokens["alice"])).status_code == 403
    )  # pending grants nothing
    lead.decide(tokens["lead"], request["request"], {"approve": True}, "d")

    for _ in range(2):  # the grant survives calls
        assert resource.put("/notes/a", headers=bearer(tokens["alice"])).status_code == 200
    assert (
        resource.get("/notes/a", headers=bearer(tokens["alice"])).status_code == 403
    )  # only write was approved
    assert resource.post("/admin", headers=bearer(tokens["alice"])).status_code == 403
    assert executed == ["write a", "write a"]


def test_iam_outage_executes_nothing_and_is_not_a_missing_grant(
    stack: dict[str, Any],
    tokens: dict[str, str],
) -> None:
    stack["server"].should_exit = True
    wait_until_refused(stack["url"])
    response = stack["client"].put("/notes/a", headers=bearer(tokens["alice"]))
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "unavailable"
    assert stack["executed"] == []


def test_undeclared_route_prevents_startup(stack: dict[str, Any]) -> None:
    app: FastAPI = stack["app"]

    @app.delete("/notes/{key}")
    async def delete_note(key: str) -> str:
        return "no"

    with pytest.raises(ManifestError, match="undeclared route DELETE /notes/{key}"):
        verify_app(stack["guard"], app)
    # and an undeclared route that is nevertheless reached fails closed
    assert stack["client"].delete("/notes/a").status_code == 403


@pytest.mark.parametrize(
    ("decided", "reason"),
    [("OTHER-TARGET", "granted"), ("n1", "unknown_operation")],  # another call's; an allow of an unknown one
)
def test_an_inconsistent_decision_is_unavailable_not_allow(
    tokens: dict[str, str],
    decided: str,
    reason: str,
) -> None:
    class Liar(IamClient):
        def authorize(self, caller_token: str | None, operation: str, target: str | None = None) -> Decision:
            return Decision("x", "alice", "notes", operation, decided, True, reason)

    guard = Guard(load_manifest(NOTES), Liar("http://127.0.0.1:1", "t"))
    with pytest.raises(Unavailable):
        guard.check(tokens["alice"], "write_note", "n1")


def test_stdlib_and_tool_adapters(stack: dict[str, Any], tokens: dict[str, str]) -> None:
    guard: Guard = stack["guard"]
    from meridian_iam.contracts import Forbidden  # noqa: PLC0415

    with pytest.raises(Forbidden):
        guard.check_http("PUT", "/notes/a", f"Bearer {tokens['alice']}")
    with pytest.raises(Forbidden):
        guard.check_http("PUT", "/notes/a/", f"Bearer {tokens['alice']}")  # trailing slash: unknown, closed
    assert guard.check_http("GET", "/health", None) is None


def build(stack: dict[str, Any], manifest: dict[str, Any]) -> tuple[FastAPI, Guard]:
    """A fresh resource app under ``manifest``, on the stack's real IAM server."""
    guard = Guard(load_manifest(manifest), stack["guard"].client)
    return FastAPI(docs_url=None, redoc_url=None, dependencies=[Depends(dependency(guard))]), guard


def declared(extra_ops: dict[str, Any], extra_http: dict[str, str]) -> dict[str, Any]:
    return {
        **MANIFEST,
        "operations": {**MANIFEST["operations"], **extra_ops},
        "http": {**MANIFEST["http"], **extra_http},
    }


def serve_notes_routes(app: FastAPI) -> None:
    """The routes of ``MANIFEST`` except the ones a test adds."""

    @app.get("/health")
    async def health() -> str:
        return "ok"

    @app.get("/notes/{key}")
    async def read_note(key: str) -> str:
        return "text"

    @app.put("/notes/{key}")
    async def write_note(key: str) -> str:
        return "ok"

    @app.post("/admin")
    async def admin() -> str:
        return "ok"


def test_a_plain_starlette_route_is_refused_at_startup_unless_it_is_a_declared_public_operation(
    stack: dict[str, Any],
) -> None:
    from starlette.responses import PlainTextResponse  # noqa: PLC0415

    # The dependency guards APIRoute handlers only, so a protected plain route would run without a credential.
    manifest = declared({"secret": {"permission": "manage"}}, {"GET /secret": "secret"})
    app, guard = build(stack, manifest)
    serve_notes_routes(app)
    app.add_route("/secret", lambda request: PlainTextResponse("leaked"), methods=["GET"])
    with pytest.raises(ManifestError, match="plain route the guard does not cover"):
        verify_app(guard, app)
    assert TestClient(app).get("/secret").status_code == 200  # which is exactly the hole the check closes
    # A plain route for a declared public operation is fine, and so is FastAPI's own /openapi.json.
    public = declared({"banner": {"public": True}}, {"GET /banner": "banner"})
    ok_app, ok_guard = build(stack, public)
    serve_notes_routes(ok_app)
    ok_app.add_route("/banner", lambda request: PlainTextResponse("hello"), methods=["GET"])
    verify_app(ok_guard, ok_app)


def test_other_route_kinds_stop_the_service_from_starting(stack: dict[str, Any]) -> None:
    from starlette.routing import Mount  # noqa: PLC0415

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401 - an ASGI app
        return None

    app, guard = build(stack, MANIFEST)
    serve_notes_routes(app)
    app.router.routes.append(Mount("/static", app=inner))
    with pytest.raises(ManifestError, match="not covered by the guard"):
        verify_app(guard, app)


def test_head_is_its_own_method_never_read_as_get(stack: dict[str, Any], tokens: dict[str, str]) -> None:
    app, guard = build(stack, MANIFEST)
    serve_notes_routes(app)

    @app.head("/notes/{key}")  # nobody declared HEAD: it must not inherit GET's authorization
    async def head_note(key: str) -> None:
        return None

    with pytest.raises(ManifestError, match="undeclared route HEAD /notes/{key}"):
        verify_app(guard, app)
    assert (
        TestClient(app).head("/notes/a", headers=bearer(tokens["alice"])).status_code == 403
    )  # unmapped: closed


def test_a_declared_head_is_authorized_under_its_own_permission(
    stack: dict[str, Any],
    tokens: dict[str, str],
) -> None:
    manifest = declared({"head_note": {"permission": "manage"}}, {"HEAD /notes/{key}": "head_note"})
    app, guard = build(stack, manifest)
    serve_notes_routes(app)

    @app.head("/notes/{key}")
    async def head_note(key: str) -> None:
        return None

    verify_app(guard, app)
    client = TestClient(app)
    assert client.head("/notes/a").status_code == 401
    assert (
        client.head("/notes/a", headers=bearer(tokens["alice"])).status_code == 403
    )  # alice may read, not "manage"


def test_an_operation_the_service_declares_and_iam_has_not_activated_is_refused_saying_so(
    stack: dict[str, Any],
    tokens: dict[str, str],
) -> None:
    app, guard = build(
        stack,
        declared({"export_notes": {"permission": "read"}}, {"GET /export": "export_notes"}),
    )
    serve_notes_routes(app)

    @app.get("/export")
    async def export_notes() -> str:
        return "every note"

    verify_app(guard, app)

    refused = TestClient(app).get("/export", headers=bearer(tokens["alice"]))  # alice may read

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert refused.json()["detail"] == {
        "error": "forbidden",
        "resource": "notes",
        "permission": None,
        "request_with": None,
        "reason": "IAM has not activated export_notes on notes",
    }


def test_an_app_that_does_not_carry_the_guards_dependency_does_not_start_even_if_its_routes_match(
    stack: dict[str, Any],
) -> None:
    guard = stack["guard"]
    bare = FastAPI(docs_url=None, redoc_url=None)  # every route matches the manifest, but nothing enforces it
    serve_notes_routes(bare)
    with pytest.raises(ManifestError, match="is not guarded"):
        verify_app(guard, bare)
    assert TestClient(bare).put("/notes/a").status_code == 200  # which is exactly what would have been served
    other = Guard(load_manifest(MANIFEST), IamClient("http://127.0.0.1:1", "t"))  # another guard's dependency
    wrong = FastAPI(docs_url=None, redoc_url=None, dependencies=[Depends(dependency(other))])
    serve_notes_routes(wrong)
    with pytest.raises(ManifestError, match="is not guarded"):
        verify_app(guard, wrong)
    right = FastAPI(docs_url=None, redoc_url=None, dependencies=[Depends(dependency(guard))])
    serve_notes_routes(right)
    verify_app(guard, right)


def test_iam_tells_anyone_how_to_ask_for_access_and_how_to_decide(iam: Iam) -> None:
    reply = TestClient(create_app(iam)).get("/")

    assert reply.status_code == HTTPStatus.OK
    routes = (
        "POST /requests",
        "Idempotency-Key",
        "Content-Type: application/json",
        "POST /requests/<id>/decision",
        "at most 500 characters",
    )
    assert all(route in reply.text for route in routes)


def test_a_malformed_request_names_bad_fields_never_contents(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    value, key = "mi_a_value_that_must_never_be_echoed", "mi_a_key_name_that_must_never_be_echoed"
    body = {"resource": "notes", "permissions": value, key: 1}

    reply = TestClient(create_app(iam)).post("/requests", headers=bearer(tokens["alice"]), json=body)

    assert reply.status_code == HTTPStatus.BAD_REQUEST
    assert reply.json()["fields"] == [
        "body.permissions: list_type",
        "body.reason: missing",
        "body: extra_forbidden",
        "header.Idempotency-Key: missing",
    ]
    assert value not in reply.text
    assert key not in reply.text


def test_a_malformed_dict_field_never_echoes_its_keys(iam: Iam, tokens: dict[str, str]) -> None:
    audience = "mi_an_audience_name_that_must_never_be_echoed"
    body = {"operation": "submit_job", "reference": "r", "purpose": "p", "grants": {}, "ttl": 60}
    body["credentials"] = {audience: 5}

    reply = TestClient(create_app(iam)).post("/workloads", headers=bearer(tokens["svc"]), json=body)

    assert reply.json()["fields"] == ["body.credentials: string_type", "header.Idempotency-Key: missing"]
    assert audience not in reply.text


def test_a_body_iam_cannot_read_says_how_to_send_it(iam: Iam, tokens: dict[str, str]) -> None:
    body = json.dumps({"resource": "notes", "permissions": ["read"], "reason": "need it"})
    headers = {
        **bearer(tokens["alice"]),
        "Idempotency-Key": "k",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    reply = TestClient(create_app(iam)).post("/requests", content=body, headers=headers)

    assert reply.status_code == HTTPStatus.BAD_REQUEST
    assert reply.json()["fields"] == ["body: send a JSON object with Content-Type: application/json"]
