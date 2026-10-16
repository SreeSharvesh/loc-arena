"""Workload identities: what a service may delegate, what a workload gets, and when it stops."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from iam_support import Clock

from meridian_iam.contracts import (
    Conflict,
    Forbidden,
    IamError,
    Invalid,
    Unauthenticated,
    Workload,
    load_manifest,
)
from meridian_iam.core import Iam

JOBS: dict[str, Any] = {
    "schema_version": 1,
    "resource": "jobs",
    "operations": {
        "submit_job": {"permission": "submit", "launch": True},
        "list_jobs": {"permission": "read"},
    },
    "tools": {"submit_job": "submit_job", "list_jobs": "list_jobs"},
}
NOTES_SECRET = "n" * 40
JOBS_SECRET = "j" * 40


@pytest.fixture
def jobs(iam: Iam, tokens: dict[str, str], clock: Clock) -> dict[str, str]:
    """A jobs service that may delegate notes read and manage and jobs submit.

    alice may submit and list jobs, write notes, and read them until ``clock.now + 100``; bob may only read
    notes.
    """
    iam.add_principal("jobs-svc", "service")
    iam.register_resource(load_manifest(JOBS), owner_team="research", approver_role="lead", max_seconds=3600)
    iam.bind_service("jobs-svc", "jobs", audience="jobs")
    iam.allow_delegation("jobs", {"notes": ["read", "manage"], "jobs": ["submit"]})
    iam.add_baseline_grant("alice", "jobs", ["submit", "read"])
    iam.add_baseline_grant("alice", "notes", ["write"])
    iam.grant(tokens["lead"], "alice", "notes", ["read"], clock.now + 100, "alice-read")
    iam.add_baseline_grant("bob", "notes", ["read"])
    return {
        "svc": iam.issue_credential("jobs-svc", ["iam"]),
        "alice": iam.issue_credential("alice", ["jobs"]),
        "bob": iam.issue_credential("bob", ["jobs"]),
    }


def issue(iam: Iam, jobs: dict[str, str], **change: Any) -> Workload:  # noqa: ANN401
    """Issue alice's job-1 through jobs-svc, with notes read for 600 s, unless ``change`` says otherwise."""
    arguments: dict[str, Any] = {
        "service_token": jobs["svc"],
        "caller_token": jobs["alice"],
        "operation": "submit_job",
        "reference": "job-1",
        "purpose": "eval run",
        "grants": {"notes": ["read"]},
        "ttl": 600,
        "credentials": {"notes": NOTES_SECRET},
        "key": "k1",
    }
    return iam.issue_workload(**{**arguments, **change})


@pytest.mark.parametrize(
    ("issuer", "delegable"),
    [
        ("missing", {"notes": ["read"]}),
        ("notes", {"missing": ["read"]}),
        ("notes", {"notes": ["fly"]}),
    ],
)
def test_delegating_an_unknown_resource_or_permission_is_invalid(
    iam: Iam,
    issuer: str,
    delegable: dict[str, list[str]],
) -> None:
    with pytest.raises(Invalid):
        iam.allow_delegation(issuer, delegable)


def test_workload_is_allowed_only_what_it_was_issued(
    iam: Iam,
    jobs: dict[str, str],
    tokens: dict[str, str],
) -> None:
    workload = issue(iam, jobs)

    read = iam.authorize(tokens["svc"], NOTES_SECRET, "read_note", "k")
    write = iam.authorize(tokens["svc"], NOTES_SECRET, "write_note", "k")

    assert (workload.sponsor, workload.grants) == ("alice", {"notes": ["read"]})
    assert (read.allow, read.principal) == (True, workload.principal)
    assert write.allow is False  # alice may write notes; her job was not given that


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            lambda jobs, tokens: {"grants": {"notes": ["write"]}},
            Forbidden,
        ),  # alice holds it; jobs may not delegate it
        (
            lambda jobs, tokens: {"grants": {"notes": ["manage"]}},
            Forbidden,
        ),  # jobs may delegate it; alice lacks it
        (lambda jobs, tokens: {"caller_token": jobs["bob"]}, Forbidden),  # bob may not submit jobs
        (
            lambda jobs, tokens: {"operation": "list_jobs"},
            Forbidden,
        ),  # alice may list jobs, but listing does not launch
        (
            lambda jobs, tokens: {"service_token": tokens["alice"]},
            Unauthenticated,
        ),  # manages IAM; not a service
        (lambda jobs, tokens: {"ttl": 3601}, Invalid),  # beyond notes' max_seconds
        (lambda jobs, tokens: {"credentials": {}}, Invalid),  # no secret for notes
        (
            lambda jobs, tokens: {"credentials": {"notes": NOTES_SECRET, "jobs": JOBS_SECRET}},
            Invalid,
        ),  # one too many
    ],
)
def test_refused_issuance_changes_nothing_and_is_audited(
    iam: Iam,
    jobs: dict[str, str],
    tokens: dict[str, str],
    change: Callable[[dict[str, str], dict[str, str]], dict[str, Any]],
    error: type[IamError],
) -> None:
    with pytest.raises(error):
        issue(iam, jobs, **change(jobs, tokens))

    kinds = [event["kind"] for event in iam.audit_log()]
    assert kinds[-1] == "rejected"
    assert "workload_issued" not in kinds
    with pytest.raises(Unauthenticated):  # no credential was registered
        iam.identify(NOTES_SECRET, "notes")


def test_retrying_with_the_same_key_returns_the_same_workload(iam: Iam, jobs: dict[str, str]) -> None:
    first = issue(iam, jobs)

    again = issue(iam, jobs)

    assert again == first


@pytest.mark.parametrize(
    "change",
    [
        lambda jobs: {"credentials": {"notes": "m" * 40}},  # same key, another secret
        lambda jobs: {"caller_token": jobs["bob"]},  # same key and secrets, another caller
        lambda jobs: {"key": "k2"},  # another key, a secret already in use
    ],
)
def test_reusing_a_key_or_a_secret_for_other_content_is_a_conflict(
    iam: Iam,
    jobs: dict[str, str],
    change: Callable[[dict[str, str]], dict[str, Any]],
) -> None:
    issue(iam, jobs)

    with pytest.raises(Conflict):
        issue(iam, jobs, **change(jobs))


def test_each_secret_works_only_at_its_own_service(iam: Iam, jobs: dict[str, str]) -> None:
    workload = issue(
        iam,
        jobs,
        grants={"notes": ["read"], "jobs": ["submit"]},
        credentials={"notes": NOTES_SECRET, "jobs": JOBS_SECRET},
    )

    assert iam.identify(NOTES_SECRET, "notes") == iam.identify(JOBS_SECRET, "jobs") == workload.principal
    with pytest.raises(Unauthenticated):
        iam.identify(NOTES_SECRET, "jobs")
    with pytest.raises(Unauthenticated):
        iam.identify(JOBS_SECRET, "notes")


def test_copied_grant_ends_with_the_sponsor_grant_while_the_credential_lives(
    iam: Iam,
    jobs: dict[str, str],
    tokens: dict[str, str],
    clock: Clock,
) -> None:
    workload = issue(iam, jobs)  # alice's notes read ends at +100; the job asked for 600 s

    clock.now += 101
    read = iam.authorize(tokens["svc"], NOTES_SECRET, "read_note", "k")

    assert workload.expires_at == 1000 + 600
    assert (read.allow, read.principal) == (False, workload.principal)


def test_workload_secrets_cannot_manage_iam(iam: Iam, jobs: dict[str, str]) -> None:
    issue(iam, jobs)

    with pytest.raises(Unauthenticated):
        iam.effective_access(NOTES_SECRET)


def test_issuance_is_audited_without_the_secrets(iam: Iam, jobs: dict[str, str]) -> None:
    workload = issue(iam, jobs)

    log = iam.audit_log()

    assert (log[-1]["kind"], log[-1]["result"]["principal"]) == ("workload_issued", workload.principal)
    assert NOTES_SECRET not in json.dumps(log)


def test_reference_and_purpose_are_kept_as_checked_trimmed(iam: Iam, jobs: dict[str, str]) -> None:
    workload = issue(iam, jobs, reference="  job-1 ", purpose=" eval run  ")

    assert (workload.reference, workload.purpose) == ("job-1", "eval run")


def test_a_workload_never_gets_an_audience_that_manages_iam(iam: Iam, jobs: dict[str, str]) -> None:
    vault = {
        "schema_version": 1,
        "resource": "vault",
        "operations": {"open": {"permission": "open"}},
        "tools": {"open": "open"},
    }
    iam.add_principal("vault-svc", "service")
    iam.register_resource(load_manifest(vault), owner_team="research", approver_role="lead", max_seconds=3600)
    iam.bind_service("vault-svc", "vault", audience="iam")  # a misconfigured binding
    iam.allow_delegation("jobs", {"vault": ["open"]})
    iam.add_baseline_grant("alice", "vault", ["open"])

    with pytest.raises(Invalid):
        issue(iam, jobs, grants={"vault": ["open"]}, credentials={"iam": "v" * 40})
