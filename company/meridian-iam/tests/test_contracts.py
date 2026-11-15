"""A release's contract: staged by the deployer alone, decided by once active, kept out when it widens."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import pytest
from fastapi.testclient import TestClient
from iam_support import NOTES

from meridian_iam.contracts import Forbidden, Invalid
from meridian_iam.core import Iam
from meridian_iam.service import create_app

EXPORT = {"permission": "read", "requestable": True}


def notes_with(operations: dict[str, Any], dropped: tuple[str, ...] = ()) -> dict[str, Any]:
    """The notes manifest with ``operations`` added or replaced, and ``dropped`` taken out."""
    kept = {name: op for name, op in NOTES["operations"].items() if name not in dropped}
    http = {route: name for route, name in NOTES["http"].items() if name not in dropped}
    http |= {f"POST /{name}": name for name in operations if name not in NOTES["operations"]}
    return {**NOTES, "operations": {**kept, **operations}, "http": http}


@pytest.fixture
def deployer(iam: Iam, tokens: dict[str, str]) -> str:
    """The deployer's credential: it alone stages notes' contracts. alice holds read on notes."""
    iam.add_principal("deployer", "service")
    iam.allow_staging("deployer", ["notes"])
    iam.add_baseline_grant("alice", "notes", ["read"])
    return iam.issue_credential("deployer", ["iam"])


@pytest.mark.parametrize(("activated", "reason"), [(False, "unknown_operation"), (True, "granted")])
def test_an_operation_a_release_adds_is_granted_to_holders_once_its_contract_is_active(
    iam: Iam,
    tokens: dict[str, str],
    deployer: str,
    *,
    activated: bool,
    reason: str,
) -> None:
    staged = iam.stage_contract(deployer, notes_with({"export_notes": EXPORT}), "deploy-1")
    if activated:
        iam.activate_contract(deployer, "notes", "deploy-1")

    decision = iam.authorize(tokens["svc"], tokens["alice"], "export_notes")

    assert staged == {"added": {"export_notes": "read"}, "changed": {}, "removed": []}
    assert (decision.allow, decision.reason) == (activated, reason)


@pytest.mark.parametrize(
    ("operations", "refusal"),
    [
        (
            {"export_notes": {"permission": "export"}},
            "export_notes needs export; the resource's are manage, read, write$",
        ),
        ({"read_note": {"permission": "manage", "requestable": True, "target": "key"}}, "read_note changes"),
        (
            {"read_note": {"permission": "read", "target": "key"}},
            "read_note changes",
        ),  # no longer requestable
        ({"export_admin": {"permission": "manage", "requestable": True}}, "makes manage requestable"),
        ({"dump": {"public": True}}, "dump is public, authenticated or a launch"),
        ({"whoami": {"authenticated": True}}, "whoami is public, authenticated or a launch"),
        ({"spawn": {"permission": "write", "launch": True}}, "spawn is public, authenticated or a launch"),
    ],
)
def test_a_contract_that_widens_changes_or_adds_a_permission_is_refused(
    iam: Iam,
    deployer: str,
    operations: dict[str, Any],
    refusal: str,
) -> None:
    with pytest.raises(Forbidden, match=refusal):
        iam.stage_contract(deployer, notes_with(operations), "deploy-1")


def test_only_the_deployer_stages_a_contract_not_the_service_a_release_runs_as(
    iam: Iam,
    tokens: dict[str, str],
    deployer: str,  # noqa: ARG001 - the deployer exists, and the service is not it
) -> None:
    with pytest.raises(Forbidden, match="notes-svc may not stage a contract for notes"):
        iam.stage_contract(tokens["svc"], notes_with({"export_notes": EXPORT}), "deploy-1")


def test_a_discarded_contract_leaves_the_active_one_and_cannot_be_activated(
    iam: Iam,
    tokens: dict[str, str],
    deployer: str,
) -> None:
    iam.stage_contract(deployer, notes_with({"export_notes": EXPORT}), "deploy-1")
    iam.discard_contract(deployer, "notes", "deploy-1")

    with pytest.raises(Invalid, match="no contract of notes is staged for deploy-1"):
        iam.activate_contract(deployer, "notes", "deploy-1")
    assert iam.authorize(tokens["svc"], tokens["alice"], "export_notes").reason == "unknown_operation"


def test_an_operation_a_later_contract_drops_is_refused_once_that_contract_is_active(
    iam: Iam,
    tokens: dict[str, str],
    deployer: str,
) -> None:
    staged = iam.stage_contract(deployer, notes_with({}, dropped=("read_note",)), "deploy-2")
    iam.activate_contract(deployer, "notes", "deploy-2")

    decision = iam.authorize(tokens["svc"], tokens["alice"], "read_note", "n1")

    assert staged == {"added": {}, "changed": {}, "removed": ["read_note"]}
    assert (decision.allow, decision.reason) == (False, "unknown_operation")


def test_a_deployment_is_staged_once(iam: Iam, deployer: str) -> None:
    iam.stage_contract(deployer, notes_with({"export_notes": EXPORT}), "deploy-1")

    with pytest.raises(Invalid, match="was staged for deploy-1 already"):
        iam.stage_contract(deployer, notes_with({}), "deploy-1")


def test_an_operation_a_release_dropped_comes_back_as_the_image_had_it(iam: Iam, deployer: str) -> None:
    iam.stage_contract(deployer, notes_with({}, dropped=("health",)), "deploy-1")
    iam.activate_contract(deployer, "notes", "deploy-1")

    change = iam.stage_contract(deployer, notes_with({}), "deploy-2")

    assert change == {"added": {"health": None}, "changed": {}, "removed": []}


@pytest.mark.parametrize(
    ("step", "resource", "reason"),
    [
        ("stage", "notes", "dump is public"),
        ("stage malformed", None, "the manifest is malformed"),  # it names no resource IAM could read
        ("activate", "notes", "no contract of notes is staged for deploy-1"),
    ],
)
def test_a_refused_step_is_an_audit_line_naming_who_asked_on_what_and_why(
    iam: Iam,
    deployer: str,
    step: str,
    resource: str | None,
    reason: str,
) -> None:
    refuse = {
        "stage": lambda: iam.stage_contract(deployer, notes_with({"dump": {"public": True}}), "deploy-1"),
        "stage malformed": lambda: iam.stage_contract(deployer, {"resource": "notes"}, "deploy-1"),
        "activate": lambda: iam.activate_contract(deployer, "notes", "deploy-1"),
    }[step]

    with pytest.raises((Forbidden, Invalid)):
        refuse()

    refused = [line for line in iam.audit_log() if line["kind"] == "contract_refused"]
    assert [(line["actor"], line["step"], line["resource"], line["deployment"]) for line in refused] == [
        ("deployer", step.split()[0], resource, "deploy-1"),
    ]
    assert reason in refused[0]["reason"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"deployment": "deploy-1", "manifest": {"resource": "notes"}}, "the manifest is malformed"),
        ({"deployment": "../deploy-1", "manifest": NOTES}, "malformed request"),
    ],
)
def test_iam_refuses_a_malformed_contract_as_invalid(
    iam: Iam,
    deployer: str,
    body: dict[str, Any],
    message: str,
) -> None:
    answer = TestClient(create_app(iam)).post(
        "/contracts",
        json=body,
        headers={"Authorization": f"Bearer {deployer}"},
    )

    assert (answer.status_code, message in answer.json()["message"]) == (HTTPStatus.BAD_REQUEST, True)


@pytest.mark.parametrize(
    ("step", "audited"),
    [("activate", "contract_activated"), ("discard", "contract_discarded")],
)
def test_a_step_asked_again_after_a_lost_reply_changes_nothing(
    iam: Iam,
    tokens: dict[str, str],
    deployer: str,
    step: str,
    audited: str,
) -> None:
    iam.stage_contract(deployer, notes_with({"export_notes": EXPORT}), "deploy-1")
    end = iam.activate_contract if step == "activate" else iam.discard_contract
    end(deployer, "notes", "deploy-1")

    end(deployer, "notes", "deploy-1")

    allowed = iam.authorize(tokens["svc"], tokens["alice"], "export_notes").allow
    steps = [line["kind"] for line in iam.audit_log() if line["kind"].startswith("contract_")]
    assert (allowed, steps) == (step == "activate", ["contract_staged", audited])


def test_a_later_contract_that_rebinds_an_added_operation_names_it_as_changed(
    iam: Iam,
    deployer: str,
) -> None:
    iam.stage_contract(deployer, notes_with({"export_notes": {"permission": "write"}}), "deploy-1")
    iam.activate_contract(deployer, "notes", "deploy-1")

    change = iam.stage_contract(deployer, notes_with({"export_notes": {"permission": "read"}}), "deploy-2")

    assert change == {"added": {}, "changed": {"export_notes": "read"}, "removed": []}
