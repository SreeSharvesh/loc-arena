"""IAM's audit log is correlation evidence, never an action: no action is counted twice."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from loc_arena.config import load_run_config
from loc_arena.recorded_events import (
    ServiceRequestRecord,
    _parse_service_request_line,
    _record_line_parser,
)

PLAIN = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def iam_config(tmp_path: Path):  # noqa: ANN201 - a RunConfig with IAM enabled
    run = tmp_path / "iam.yaml"
    run.write_text("extends: aurora-efficiency.deterministic.yaml\niam:\n  enabled: true\n")
    return load_run_config(run, Path("configs"))


AGENTS = ("agent-main", "serving-agent")


def parse(config, event: Mapping[str, Any]):  # noqa: ANN001, ANN201 - the parsed line, or None
    iam = next(service for service in config.live_services if service.name == "iam")
    return _record_line_parser(iam, config)(json.dumps(event), AGENTS)


def test_an_authorization_decision_is_correlation_only_so_no_action_is_counted_twice(tmp_path: Path) -> None:
    config = iam_config(tmp_path)
    decision = {"at": 1, "kind": "authorize", "actor": "agent-main", "allow": True, "decision": "d"}
    assert parse(config, decision) is None  # the resource's own record of the call is the action
    agents = next(service for service in config.live_services if service.name == "agents")
    assert _record_line_parser(agents, config) is _parse_service_request_line


def test_a_direct_management_mutation_is_an_action_of_the_agent_that_made_it(tmp_path: Path) -> None:
    config = iam_config(tmp_path)
    grant = {
        "at": 1_000,
        "kind": "grant",
        "actor": "agent-main",
        "via": None,
        "principal": "serving-agent",
        "resource": "notes",
        "permissions": ["write"],
        "until": None,
        "result": {"principal": "serving-agent"},
    }
    parsed = parse(config, grant)
    assert parsed is not None and parsed.agent == "agent-main" and parsed.wall == 1_000
    (event,) = parsed.events
    assert (event.kind, event.actor_uid, event.target_id, event.tool) == (
        "action",
        "agent-main",
        "serving-agent",
        "iam_grant",
    )
    assert event.payload["blocked"] is False and event.payload["args"]["permissions"] == ["write"]
    assert "result" not in event.payload["args"] and "via" not in event.payload["args"]


def test_an_access_request_keeps_the_request_id_iam_answered(tmp_path: Path) -> None:
    config = iam_config(tmp_path)
    request = {
        "at": 1_000,
        "kind": "access_requested",
        "actor": "serving-agent",
        "via": None,
        "resource": "notes",
        "permissions": ["read"],
        "reason": "need it",
        "until": None,
        "result": {"request": "r-1", "state": "pending"},
    }

    parsed = parse(config, request)

    assert parsed is not None
    assert parsed.events[0].result == {"request": "r-1", "state": "pending"}


def test_every_kind_of_management_mutation_is_recorded_and_a_refused_one_is_blocked(tmp_path: Path) -> None:
    config = iam_config(tmp_path)
    for kind in (
        "access_requested",
        "access_approved",
        "access_denied",
        "grant",
        "revoke",
        "member_added",
        "member_removed",
        "principal_disabled",
    ):
        parsed = parse(
            config,
            {"at": 5, "kind": kind, "actor": "agent-main", "via": None, "resource": "notes"},
        )
        assert parsed is not None and parsed.events[0].tool == f"iam_{kind}"
    refused = parse(
        config,
        {
            "at": 5,
            "kind": "rejected",
            "actor": "serving-agent",
            "operation": "grant",
            "code": "forbidden",
            "via": None,
        },
    )
    assert refused is not None
    (event,) = refused.events
    assert (event.tool, event.payload["blocked"], event.payload["reason"]) == ("iam_grant", True, "forbidden")
    assert (
        parse(config, {"at": 5, "kind": "credential_renewed", "actor": "agent-main"}) is None
    )  # identity upkeep, not control-plane change


def test_a_mutation_forwarded_by_a_service_that_records_its_own_calls_is_not_counted_twice(
    tmp_path: Path,
) -> None:
    config = iam_config(tmp_path)
    forwarded = {
        "at": 5,
        "kind": "grant",
        "actor": "agent-main",
        "resource": "agents",
        "principal": "serving-agent",
    }
    assert (
        parse(config, {**forwarded, "via": "agents"}) is None
    )  # the agents service's audit line is the action
    assert (
        parse(config, {**forwarded, "via": "tickets"}) is not None
    )  # a service not marked records_actions writes no such record: IAM's is it


def test_an_approvers_decision_is_no_agents_action_and_is_expected(tmp_path: Path) -> None:
    config = iam_config(tmp_path)
    decision = {"at": 7, "kind": "access_approved", "actor": "notes-owner", "request": "r1", "result": {}}

    assert parse(config, decision) is None  # the notes approver: neither placed nor reported unplaced


def test_a_malformed_iam_line_is_not_an_action(tmp_path: Path) -> None:
    config = iam_config(tmp_path)
    assert parse(config, {"kind": "grant", "via": None}) is None  # no actor, no time
    assert parse(config, {"at": True, "kind": "grant", "actor": "agent-main", "via": None}) is None


def test_a_service_named_iam_is_an_ordinary_service_when_the_run_does_not_enable_iam() -> None:
    agents = next(service for service in PLAIN.live_services if service.name == "agents")
    assert _record_line_parser(agents, PLAIN) is _parse_service_request_line


def test_a_services_request_record_carries_the_decision_that_joins_it_to_iams_event() -> None:
    line = {
        "wall_ts": "2026-10-13T12:00:00+00:00",
        "caller": "serving-agent",
        "method": "POST",
        "status": 403,
        "operation": "register_agent",
        "arguments": {},
        "decision": "d1",
    }
    assert ServiceRequestRecord.model_validate_json(json.dumps(line)).decision == "d1"
    assert ServiceRequestRecord.model_validate_json(json.dumps({**line, "decision": None})).decision is None


def test_every_kind_in_the_set_is_covered_by_the_mutation_test_above() -> None:
    from loc_arena.recorded_events import _IAM_MUTATIONS  # noqa: PLC0415

    assert _IAM_MUTATIONS == {
        "access_requested",
        "access_approved",
        "access_denied",
        "grant",
        "revoke",
        "member_added",
        "member_removed",
        "principal_disabled",
    }  # adding a kind to the set should mean adding it to the test above


@pytest.mark.parametrize(
    "odd",
    [
        {"kind": []},  # unhashable: a list in a set lookup raises TypeError
        {"kind": {}},
        {"kind": "rejected", "operation": []},
        {"kind": "rejected", "operation": {"a": 1}},
        {"kind": "grant", "actor": ["agent-main"]},
        {"kind": "grant", "at": "soon"},
        {"kind": "grant", "at": [1]},
    ],
)
def test_a_line_with_an_unexpected_json_shape_is_skipped_never_fatal(
    tmp_path: Path,
    odd: dict[str, Any],
) -> None:
    config = iam_config(tmp_path)
    base = {"at": 5, "kind": "grant", "actor": "agent-main", "via": None, "resource": "notes"}
    # ``build_recorded_events`` would otherwise abort for the whole run on one odd line
    assert parse(config, {**base, **odd}) is None


@pytest.mark.parametrize("via", [[], {"x": 1}, 7])
def test_an_unusable_via_does_not_hide_a_real_mutation_it_is_recorded(tmp_path: Path, via: object) -> None:
    config = iam_config(tmp_path)
    parsed = parse(config, {"at": 5, "kind": "grant", "actor": "agent-main", "via": via, "resource": "notes"})
    assert (
        parsed is not None and parsed.events[0].tool == "iam_grant"
    )  # never lose a mutation over an odd field
