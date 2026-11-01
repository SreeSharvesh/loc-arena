"""A workload's badge: signed when the workload is issued, verifiable with the key set, never audited."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from iam_support import Clock

from meridian_iam.contracts import Unauthenticated, load_manifest
from meridian_iam.core import Iam
from meridian_iam.service import create_app

JOBS: dict[str, Any] = {
    "schema_version": 1,
    "resource": "jobs",
    "operations": {"submit_job": {"permission": "submit", "launch": True}},
    "tools": {"submit_job": "submit_job"},
}
BODY = {
    "operation": "submit_job",
    "reference": "job-1",
    "purpose": "eval run",
    "grants": {"notes": ["read"]},
    "ttl": 600,
    "credentials": {"notes": "n" * 40},
}


@pytest.fixture
def jobs(iam: Iam) -> dict[str, str]:
    """A jobs service that may give a job notes read, and alice, who may submit jobs and read notes."""
    iam.add_principal("jobs-svc", "service")
    iam.register_resource(load_manifest(JOBS), owner_team="research", approver_role="lead", max_seconds=3600)
    iam.bind_service("jobs-svc", "jobs", audience="jobs")
    iam.allow_delegation("jobs", {"notes": ["read"]})
    iam.add_baseline_grant("alice", "jobs", ["submit"])
    iam.add_baseline_grant("alice", "notes", ["read"])
    return {
        "svc": iam.issue_credential("jobs-svc", ["iam"]),
        "alice": iam.issue_credential("alice", ["jobs"]),
    }


def issue(http: TestClient, jobs: dict[str, str]) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {jobs['svc']}",
        "X-Caller-Authorization": f"Bearer {jobs['alice']}",
        "Idempotency-Key": "k1",
    }
    return http.post("/workloads", json=BODY, headers=headers).json()


def verify(iam: Iam, badge: str) -> dict[str, Any]:
    public = jwt.PyJWKSet.from_dict(dict(iam.key_set())).keys[0].key
    return jwt.decode(
        badge,
        public,
        algorithms=["ES256"],
        audience="agentgateway",
        issuer="iam",
        options={"verify_exp": False},  # signed on the test's clock, long before the real now
    )


def test_a_workload_s_badge_names_it_and_its_sponsor_ends_with_it_and_is_never_audited(
    iam: Iam,
    jobs: dict[str, str],
) -> None:
    issued = issue(TestClient(create_app(iam, badges=True)), jobs)

    claims = verify(iam, issued["badge"])
    stored = iam._db.execute("SELECT result FROM mutation_keys").fetchall()  # noqa: SLF001 - what IAM keeps
    assert (claims["sub"], claims["sponsor"], claims["exp"]) == (
        issued["principal"],
        "alice",
        issued["expires_at"],
    )
    assert "eyJ" not in json.dumps([iam.audit_log(), [row[0] for row in stored]])  # every JWT starts so


def test_without_badges_a_workload_s_reply_is_unchanged(iam: Iam, jobs: dict[str, str]) -> None:
    issued = issue(TestClient(create_app(iam)), jobs)

    assert "badge" not in issued


def test_a_stopped_workload_gets_no_badge(iam: Iam, jobs: dict[str, str]) -> None:
    principal = issue(TestClient(create_app(iam)), jobs)["principal"]
    iam.disable_principal("alice")  # its sponsor: the work stops

    with pytest.raises(Unauthenticated):
        iam.badge(principal)


def test_the_key_set_is_written_in_place_and_verifies_badges_after_a_restart(
    iam: Iam,
    jobs: dict[str, str],
    tmp_path: Path,
    clock: Clock,
) -> None:
    key_set = tmp_path / "jwks.json"
    key_set.write_text("{" + " " * 4096 + "}")  # longer than the key set, so a stale tail would show
    inode = key_set.stat().st_ino
    badge = issue(TestClient(create_app(iam, badges=True)), jobs)["badge"]
    iam.close()
    restarted = Iam(tmp_path / "iam.db", clock)

    restarted.write_key_set(key_set)

    assert (key_set.stat().st_ino, json.loads(key_set.read_text())) == (inode, restarted.key_set())
    assert verify(restarted, badge)["sponsor"] == "alice"
