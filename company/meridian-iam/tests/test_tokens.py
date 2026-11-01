"""Caller and service tokens: issued over ``POST /token``, accepted only by their own service, while live."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from iam_support import Clock

from meridian_iam.contracts import Invalid, Unauthenticated, load_manifest
from meridian_iam.core import Iam
from meridian_iam.service import TokenEndpoint, create_app

DOOR_SECRET = "d" * 40
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
DOOR = {"Authorization": "Basic " + base64.b64encode(f"agentgateway:{DOOR_SECRET}".encode()).decode()}
JOBS: dict[str, Any] = {
    "schema_version": 1,
    "resource": "jobs",
    "operations": {"submit_job": {"permission": "submit", "launch": True}},
    "tools": {"submit_job": "submit_job"},
}


@pytest.fixture
def http(iam: Iam) -> TestClient:
    return TestClient(create_app(iam, TokenEndpoint(caller_token_seconds=3600, exchange_secret=DOOR_SECRET)))


@pytest.fixture
def jobs(iam: Iam) -> str:
    """A jobs service that may give a job notes read; it returns the service's ``iam`` credential."""
    iam.add_principal("jobs-svc", "service")
    iam.register_resource(load_manifest(JOBS), owner_team="research", approver_role="lead", max_seconds=3600)
    iam.bind_service("jobs-svc", "jobs", audience="jobs")
    iam.allow_delegation("jobs", {"notes": ["read"]})
    return iam.issue_credential("jobs-svc", ["iam"])


def token(http: TestClient, subject: str, audience: str, headers: dict[str, str] | None = None) -> Any:  # noqa: ANN401
    form = {"grant_type": EXCHANGE, "subject_token": subject, "audience": audience}
    return http.post("/token", data=form, headers=headers or {})


def service_token(http: TestClient, credential: str, audience: str = "notes") -> str:
    caller = token(http, credential, "agentgateway").json()["access_token"]
    return token(http, caller, audience, DOOR).json()["access_token"]


def test_the_door_exchanges_a_caller_token_for_one_iam_decides_with(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
) -> None:
    iam.add_baseline_grant("alice", "notes", ["read"])
    caller = token(http, tokens["alice"], "agentgateway").json()["access_token"]
    exchanged = token(http, caller, "notes", DOOR).json()

    decision = iam.authorize(tokens["svc"], exchanged["access_token"], "read_note", "k")

    assert (decision.principal, decision.allow) == ("alice", True)
    assert (exchanged["token_type"], exchanged["expires_in"]) == ("Bearer", 300)


def test_only_the_door_exchanges_and_only_an_iam_credential_becomes_a_caller_token(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
) -> None:
    caller = token(http, tokens["alice"], "agentgateway").json()["access_token"]
    per_service = iam.issue_credential("alice", ["notes"])  # what a service or the tools gateway holds

    without_door = token(http, caller, "notes")
    wrong_secret = token(http, caller, "notes", {"Authorization": DOOR["Authorization"][:-4] + "AAAA"})
    swapped = token(http, per_service, "agentgateway")
    managing_iam = token(http, caller, "iam", DOOR)

    assert (without_door.status_code, without_door.json()["error"]) == (401, "invalid_client")
    assert wrong_secret.status_code == 401
    assert (swapped.status_code, swapped.json()["error"]) == (400, "invalid_grant")
    assert (managing_iam.status_code, managing_iam.json()["error"]) == (400, "invalid_grant")


def test_a_service_token_works_at_its_own_service_alone_and_never_manages_iam(
    tokens: dict[str, str],
    http: TestClient,
    jobs: str,
) -> None:
    notes = service_token(http, tokens["alice"])

    at_jobs = http.get(
        "/workloads/delegable",
        params={"operation": "submit_job"},
        headers={
            "Authorization": f"Bearer {jobs}",
            "X-Caller-Authorization": f"Bearer {notes}",
        },
    )
    at_iam = http.get("/access", headers={"Authorization": f"Bearer {notes}"})

    assert at_jobs.status_code == 401
    assert at_iam.status_code == 401


def test_a_service_token_stops_when_its_principal_is_disabled(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
) -> None:
    iam.add_baseline_grant("alice", "notes", ["read"])
    notes = service_token(http, tokens["alice"])
    iam.disable_principal("alice")

    reply = http.post(
        "/authorize",
        json={"operation": "read_note", "target": "k"},
        headers={"Authorization": f"Bearer {tokens['svc']}", "X-Caller-Authorization": f"Bearer {notes}"},
    )

    assert reply.status_code == 401


def test_a_token_request_past_its_size_bound_is_refused_unread(
    tokens: dict[str, str],
    http: TestClient,
) -> None:
    padding = "x" * 8192

    reply = http.post(
        "/token",
        data={"grant_type": EXCHANGE, "subject_token": tokens["alice"], "pad": padding},
    )

    assert (reply.status_code, reply.json()["error"]) == (400, "invalid_request")


def test_an_opaque_credential_with_a_dot_is_refused_at_import(iam: Iam) -> None:
    with pytest.raises(Invalid):
        iam.import_credential("alice", "a.b.c" + "x" * 40, ["notes"])


def test_a_caller_token_reaches_only_the_services_its_caller_s_credentials_name(
    tokens: dict[str, str],
    http: TestClient,
    jobs: str,  # binds the jobs audience; alice holds credentials for iam and notes alone
) -> None:
    caller = token(http, tokens["alice"], "agentgateway").json()["access_token"]

    reply = token(http, caller, "jobs", DOOR)

    assert (reply.status_code, reply.json()["error"]) == (400, "invalid_grant")


def test_rotating_the_credential_ends_the_tokens_swapped_for_it(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
) -> None:
    caller = token(http, tokens["alice"], "agentgateway").json()["access_token"]
    notes = token(http, caller, "notes", DOOR).json()["access_token"]
    iam.rotate_credential("alice")

    exchanged = token(http, caller, "notes", DOOR)

    assert exchanged.status_code == 400
    with pytest.raises(Unauthenticated):
        iam.identify(notes, "notes")


def test_a_caller_token_in_its_allowance_gets_a_service_token_no_longer_than_it(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
    clock: Clock,
) -> None:
    caller = token(http, tokens["alice"], "agentgateway").json()["access_token"]
    clock.now += 3600 + 59  # one second left of the caller token's allowance
    notes = token(http, caller, "notes", DOOR).json()["access_token"]
    clock.now += 1

    with pytest.raises(Unauthenticated):
        iam.identify(notes, "notes")


@pytest.mark.parametrize(("past_expiry", "allowed"), [(59, True), (60, False)])
def test_a_service_token_is_accepted_for_the_door_s_60_seconds_past_its_expiry(  # noqa: PLR0913
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
    clock: Clock,
    past_expiry: int,
    allowed: bool,
) -> None:
    iam.add_baseline_grant("alice", "notes", ["read"])
    notes = service_token(http, tokens["alice"])
    clock.now += 300 + past_expiry

    reply = http.post(
        "/authorize",
        json={"operation": "read_note", "target": "k"},
        headers={"Authorization": f"Bearer {tokens['svc']}", "X-Caller-Authorization": f"Bearer {notes}"},
    )

    assert (reply.status_code == 200) is allowed


def test_a_workload_s_caller_token_names_its_sponsor_works_at_its_services_and_is_never_audited(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
    jobs: str,
) -> None:
    iam.add_baseline_grant("alice", "jobs", ["submit"])
    iam.add_baseline_grant("alice", "notes", ["read"])
    sponsor = iam.issue_credential("alice", ["iam", "jobs"])
    body = {
        "operation": "submit_job",
        "reference": "job-1",
        "purpose": "eval run",
        "grants": {"notes": ["read"]},
        "ttl": 600,
        "credentials": {"notes": "n" * 40},
    }

    issued = http.post(
        "/workloads",
        json=body,
        headers={
            "Authorization": f"Bearer {jobs}",
            "X-Caller-Authorization": f"Bearer {service_token(http, sponsor, 'jobs')}",
            "Idempotency-Key": "k1",
        },
    ).json()

    claims = jwt.decode(issued["caller_token"], options={"verify_signature": False})
    notes = token(http, issued["caller_token"], "notes", DOOR).json()["access_token"]
    stored = iam._db.execute("SELECT result FROM mutation_keys").fetchall()  # noqa: SLF001 - what IAM keeps
    assert (claims["sub"], claims["sponsor"], claims["exp"]) == (
        issued["principal"],
        "alice",
        issued["expires_at"],
    )
    assert iam.authorize(tokens["svc"], notes, "read_note", "k").principal == issued["principal"]
    assert "eyJ" not in json.dumps([iam.audit_log(), [row[0] for row in stored]])  # every JWT starts so


def test_a_refused_token_before_any_signing_does_not_lose_the_key(
    tmp_path: Path,
    clock: Clock,
) -> None:
    fresh = Iam(tmp_path / "fresh.db", clock)
    fresh.add_principal("carol", "agent")
    with pytest.raises(Unauthenticated):
        fresh.identify("a.b.c", "notes")  # the first call that needs the key fails
    caller, _ = fresh.caller_token(fresh.issue_credential("carol", ["iam"]), 600)
    fresh.close()

    restarted = Iam(tmp_path / "fresh.db", clock)

    public = jwt.PyJWKSet.from_dict(dict(restarted.key_set())).keys[0].key
    claims = jwt.decode(
        caller, public, algorithms=["ES256"], audience="agentgateway", options={"verify_exp": False},
    )
    assert claims["sub"] == "carol"


def test_the_key_set_is_written_in_place_verifies_caller_tokens_and_survives_a_restart(
    iam: Iam,
    tokens: dict[str, str],
    http: TestClient,
    tmp_path: Path,
    clock: Clock,
) -> None:
    key_set = tmp_path / "jwks.json"
    key_set.write_text("{}")
    inode = key_set.stat().st_ino
    caller = token(http, tokens["alice"], "agentgateway").json()["access_token"]
    iam.close()
    restarted = Iam(tmp_path / "iam.db", clock)

    restarted.write_key_set(key_set)

    public = jwt.PyJWKSet.from_json(key_set.read_text()).keys[0].key
    claims = jwt.decode(
        caller,
        public,
        algorithms=["ES256"],
        audience="agentgateway",
        issuer="iam",
        options={"verify_exp": False},  # issued on the test's clock, long before the real now
    )
    assert (key_set.stat().st_ino, claims["sub"]) == (inode, "alice")
