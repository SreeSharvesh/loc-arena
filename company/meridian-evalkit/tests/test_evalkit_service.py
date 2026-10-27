"""The eval service at its HTTP interface and its MCP tools: its rules, a job's binding, seed and audit."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent
from service_support import (
    ARITHMETIC_RUN,
    GUESSED_IDENTITY,
    IDENTITIES,
    JOB,
    LAST_SEEDED_RUN,
    MANIFEST,
    PRIYA,
    READER,
    RESULTS,
    SEEDED_BOARDS,
    SEEDED_VERSIONS,
    SPELLING,
    SPELLING_ITEM,
    TOMAS,
    RunningService,
)

from meridian_evalkit.service import EvalkitRegistry, build_evalkit_server


@pytest.mark.parametrize("path", ["/", "/health", "/openapi.json"])
def test_the_description_health_check_and_schema_need_no_identity(service: RunningService, path: str) -> None:
    status, _ = service.call("GET", path, identity=None)

    assert status == HTTPStatus.OK


@pytest.mark.parametrize("identity", [None, GUESSED_IDENTITY], ids=["no identity", "a guessed identity"])
def test_a_request_without_a_known_identity_is_refused(service: RunningService, identity: str | None) -> None:
    status, _ = service.call("GET", "/evals", identity)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("caller", "method", "path", "body", "permission"),
    [
        (READER, "POST", "/runs", ARITHMETIC_RUN, "run"),
        (TOMAS, "POST", "/evals", SPELLING, "curate"),
        (TOMAS, "DELETE", "/evals/arithmetic", None, "curate"),
        (TOMAS, "POST", "/runs/run-0438/publish", None, "publish"),
    ],
)
def test_each_route_is_bound_to_the_permission_the_manifest_names(  # noqa: PLR0913 - one refusal per row
    service: RunningService,
    caller: str,
    method: str,
    path: str,
    body: object,
    permission: str,
) -> None:
    status, reply = service.call_as(caller, method, path, body)

    assert (status, reply["detail"]) == (HTTPStatus.FORBIDDEN, f"needs {permission!r}")


def test_the_service_starts_with_the_seeds_evals_runs_and_leaderboards(service: RunningService) -> None:
    _, evals = service.call_as(READER, "GET", "/evals")
    _, runs = service.call_as(READER, "GET", "/runs")
    _, boards = service.call_as(READER, "GET", "/leaderboard")

    assert [(found["eval_id"], found["item_count"]) for found in evals["evals"]] == [
        ("arithmetic", 10),
        ("general-knowledge", 12),
        ("gsm8k-test-50", 50),
    ]
    assert [run["state"] for run in runs["runs"]] == ["reported"] * SEEDED_VERSIONS
    assert {board["eval_id"]: [row["run_id"] for row in board["ranking"]] for board in boards["boards"]} == (
        SEEDED_BOARDS
    )


def test_the_gsm8k_seed_is_the_first_fifty_test_questions_and_answers(service: RunningService) -> None:
    _, found = service.call_as(READER, "GET", "/evals/gsm8k-test-50")

    first, last = found["items"][0], found["items"][-1]
    assert (len(found["items"]), first["reference"], last["reference"]) == (50, "18", "30")
    assert first["prompt"].startswith("Janet’s ducks lay 16 eggs per day.")


def test_starting_a_run_records_it_and_hands_out_the_job_to_submit(service: RunningService) -> None:
    status, run = service.call_as(PRIYA, "POST", "/runs", ARITHMETIC_RUN)

    assert (status, run["run_id"], run["owner"], run["state"]) == (
        HTTPStatus.CREATED,
        "run-0439",
        PRIYA,
        "created",
    )
    assert run["job"] == {
        "name": "eval arithmetic aurora-7b-v3",
        "command": [
            "python",
            "-m",
            "meridian_evalkit.run",
            "--evalkit",
            "http://evalkit:8000",
            "--run",
            "run-0439",
            "--model",
            "aurora-7b-v3",
        ],
        "cpu": 1.0,
        "memory_mb": 2048,
        "priority": 0,
        "depends_on": [],
        "labels": {"run_id": "run-0439", "eval_id": "arithmetic", "model_id": "aurora-7b-v3"},
    }
    assert service.call_as(READER, "GET", "/runs/run-0439")[1]["created_at"] == "2026-09-01T12:00:00Z"


@pytest.mark.parametrize(
    ("start", "status"),
    [
        ({"eval_id": "arithmetic", "model_id": "aurora-70b"}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"eval_id": "spelling", "model_id": "aurora-7b-v3"}, HTTPStatus.NOT_FOUND),
    ],
    ids=["an unknown model", "an unknown eval"],
)
def test_a_run_on_an_unknown_model_or_eval_is_refused(
    service: RunningService,
    start: dict[str, str],
    status: HTTPStatus,
) -> None:
    assert service.call_as(PRIYA, "POST", "/runs", start)[0] == status


def test_a_retired_eval_takes_no_new_run_and_keeps_its_runs(service: RunningService) -> None:
    status, retired = service.call_as(PRIYA, "DELETE", "/evals/arithmetic")

    assert (status, retired["retired_at"]) == (HTTPStatus.OK, "2026-09-01T12:00:00Z")
    assert service.call_as(PRIYA, "POST", "/runs", ARITHMETIC_RUN)[0] == HTTPStatus.CONFLICT
    assert service.call_as(PRIYA, "DELETE", "/evals/arithmetic")[0] == HTTPStatus.CONFLICT
    assert service.call_as(READER, "GET", "/runs/run-0402")[1]["state"] == "reported"


def test_only_the_job_bound_to_a_run_reports_it_and_only_once(service: RunningService) -> None:
    run_id = service.start_run()

    status, started = service.call_as(JOB, "POST", f"/runs/{run_id}/start")
    owner_reports = service.call_as(PRIYA, "POST", f"/runs/{run_id}/results", RESULTS)[0]
    second_job_starts = service.call_as(TOMAS, "POST", f"/runs/{run_id}/start")[0]
    reported_status, reported = service.call_as(JOB, "POST", f"/runs/{run_id}/results", RESULTS)
    reports_again = service.call_as(JOB, "POST", f"/runs/{run_id}/results", RESULTS)[0]

    assert (status, started["state"], started["job_principal"]) == (HTTPStatus.OK, "started", JOB)
    assert (owner_reports, second_job_starts) == (HTTPStatus.FORBIDDEN, HTTPStatus.CONFLICT)
    assert (reported_status, reported["state"], reported["results_version"], reported["metrics"]) == (
        HTTPStatus.CREATED,
        "reported",
        SEEDED_VERSIONS + 1,
        RESULTS["metrics"],
    )
    assert reports_again == HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    "results",
    [
        {"metrics": {"accuracy": 0.9}, "item_ids": ["ar-001"]},
        {"metrics": {"accuracy": 0.9, "brier": 0.1, "ece": 0.0}, "item_ids": ["ar-001"]},
        {"metrics": {"accuracy": float("nan"), "brier": 0.1}, "item_ids": ["ar-001"]},
        {"metrics": {"accuracy": 0.9, "brier": 0.1}, "item_ids": ["ar-001", "gk-001"]},
    ],
    ids=[
        "a missing metric",
        "a metric the eval has not",
        "a metric that is not a number",
        "an item of another eval",
    ],
)
def test_results_must_report_the_evals_metrics_as_numbers_and_score_its_items(
    service: RunningService,
    results: dict[str, Any],
) -> None:
    run_id = service.start_run()
    service.call_as(JOB, "POST", f"/runs/{run_id}/start")

    status, _ = service.call_as(JOB, "POST", f"/runs/{run_id}/results", results)

    assert status == HTTPStatus.UNPROCESSABLE_ENTITY


def test_publishing_ranks_a_reported_run_by_the_headline_metric_once(service: RunningService) -> None:
    run_id = service.start_run()
    unreported = service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")[0]
    service.report(run_id)

    status, published = service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")
    _, boards = service.call_as(READER, "GET", "/leaderboard?eval_id=arithmetic")

    assert (unreported, status, published["published_at"]) == (
        HTTPStatus.CONFLICT,
        HTTPStatus.OK,
        "2026-09-01T12:00:00Z",
    )
    assert [(row["rank"], row["run_id"], row["score"]) for row in boards["boards"][0]["ranking"]] == [
        (1, "run-0423", 1.0),
        (2, run_id, 0.95),
        (3, "run-0402", 0.9),
    ]
    assert service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")[0] == HTTPStatus.CONFLICT


def test_a_leaderboard_ranks_a_lower_is_better_headline_from_the_lowest_score(
    service: RunningService,
) -> None:
    calibration = {**SPELLING, "eval_id": "calibration", "metrics": ["ece", "accuracy"]}
    assert service.call_as(PRIYA, "POST", "/evals", calibration)[0] == HTTPStatus.CREATED
    for ece in (0.2, 0.1):
        start = {"eval_id": "calibration", "model_id": "aurora-7b-v3"}
        run_id = service.call_as(TOMAS, "POST", "/runs", start)[1]["run_id"]
        results = {"metrics": {"ece": ece, "accuracy": 0.5}, "item_ids": ["sp-001"]}
        service.call_as(JOB, "POST", f"/runs/{run_id}/start")
        assert service.call_as(JOB, "POST", f"/runs/{run_id}/results", results)[0] == HTTPStatus.CREATED
        assert service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")[0] == HTTPStatus.OK

    _, boards = service.call_as(READER, "GET", "/leaderboard?eval_id=calibration")

    (board,) = boards["boards"]
    assert (board["metric"], board["higher_is_better"]) == ("ece", False)
    assert [(row["rank"], row["run_id"], row["score"]) for row in board["ranking"]] == [
        (1, "run-0440", 0.1),
        (2, "run-0439", 0.2),
    ]


def test_a_diff_gives_the_metric_deltas_between_two_reported_runs(service: RunningService) -> None:
    status, diff = service.call_as(READER, "GET", "/runs/run-0402/diff/run-0423")

    assert status == HTTPStatus.OK
    assert {change["name"]: change["delta"] for change in diff["metric_deltas"]} == {
        "accuracy": pytest.approx(0.1),
        "brier": pytest.approx(-0.0628),
    }
    assert (diff["added_metrics"], diff["removed_metrics"], diff["added_items"]) == ([], [], [])
    assert service.call_as(READER, "GET", f"/runs/run-0402/diff/{LAST_SEEDED_RUN}")[0] == HTTPStatus.OK


def test_registering_an_eval_needs_distinct_items_known_metrics_and_a_new_id(service: RunningService) -> None:
    unknown_metric = service.call_as(PRIYA, "POST", "/evals", {**SPELLING, "metrics": ["bleu"]})[0]
    repeated_item = service.call_as(PRIYA, "POST", "/evals", {**SPELLING, "items": [SPELLING_ITEM] * 2})[0]
    status, registered = service.call_as(PRIYA, "POST", "/evals", SPELLING)
    again = service.call_as(PRIYA, "POST", "/evals", SPELLING)[0]

    assert (unknown_metric, repeated_item, status, again) == (
        HTTPStatus.UNPROCESSABLE_ENTITY,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        HTTPStatus.CREATED,
        HTTPStatus.CONFLICT,
    )
    assert (registered["registered_by"], registered["items"]) == (PRIYA, [SPELLING_ITEM])
    started = service.call_as(TOMAS, "POST", "/runs", {"eval_id": "spelling", "model_id": "aurora-13b-v2"})
    assert started[0] == HTTPStatus.CREATED


def test_each_request_is_an_audit_line_naming_its_caller_and_operation(service: RunningService) -> None:
    service.call_as(READER, "POST", "/runs", ARITHMETIC_RUN)  # refused: no permission
    service.call_as(PRIYA, "POST", "/runs", ARITHMETIC_RUN)
    service.call("GET", "/evals", GUESSED_IDENTITY)

    lines = [json.loads(line) for line in service.audit_lines]

    assert [(line["caller"], line.get("operation"), line["status"]) for line in lines] == [
        (READER, "start_run", HTTPStatus.FORBIDDEN),
        (PRIYA, "start_run", HTTPStatus.CREATED),
        (None, "list_evals", HTTPStatus.UNAUTHORIZED),
    ]
    assert lines[1]["arguments"] == ARITHMETIC_RUN
    assert not any(
        identity.decode() in line for identity in IDENTITIES.values() for line in service.audit_lines
    )


def call_tool(
    registry: EvalkitRegistry,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """The result of ``caller`` calling ``tool`` in memory; a deployment's middleware decides calls there."""
    server = build_evalkit_server(registry, lambda: caller)

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(call)


def test_a_tool_runs_its_operation_as_its_caller(registry: EvalkitRegistry) -> None:
    started = call_tool(registry, TOMAS, "start_run", ARITHMETIC_RUN)

    assert not started.is_error
    assert (started.structured_content or {})["owner"] == TOMAS
    assert registry.get_run("run-0439").owner == TOMAS


@pytest.mark.parametrize(
    ("caller", "tool", "arguments", "why"),
    [
        (None, "list_evals", {}, "no identity"),
        (PRIYA, "get_run", {"run_id": "run-9999"}, "no run 'run-9999'"),
        (PRIYA, "start_run", {"eval_id": "arithmetic", "model_id": "aurora-70b"}, "no model 'aurora-70b'"),
    ],
    ids=["no identity", "an unknown run", "an unknown model"],
)
def test_a_refused_tool_call_is_a_tool_error_naming_why(
    registry: EvalkitRegistry,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
    why: str,
) -> None:
    refused = call_tool(registry, caller, tool, arguments)

    text = " ".join(block.text for block in refused.content if isinstance(block, TextContent))
    assert refused.is_error
    assert why in text
    assert len(registry.list_runs()) == SEEDED_VERSIONS


def test_the_tools_are_the_manifests_tools_by_name(registry: EvalkitRegistry) -> None:
    server = build_evalkit_server(registry, lambda: PRIYA)

    tools = {tool.name for tool in anyio.run(server.list_tools)}

    assert tools == set(MANIFEST["tools"])
