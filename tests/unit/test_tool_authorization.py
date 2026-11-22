"""The tools gateway's line of a call it asked IAM about, and what the action monitor reads of such a call.

The line is read as the record a service writes of a call. Each line here is one the pinned gateway wrote in
a stack run, cut to the fields the reader takes.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping

import pytest
from loc_arena.config import load_run_config
from loc_arena.logging_.events import Event
from loc_arena.monitors.action_monitor import ActionMonitor
from loc_arena.monitors.registry import MonitorContext, load_monitor_specs
from loc_arena.tool_authorization import read_enforced_call

REFUSAL = "not permitted: read on telemetry. Ask for it with: POST /requests on the IAM service."
REFUSALS = {"execute_sql": REFUSAL}
SERVICES = ("telemetry",)
QUERY = {"query": "SELECT body FROM opentelemetry_logs"}
ASKED = {"requested_method": "tools/call", "requested_tool": "execute_sql", "requested_args": QUERY}
REQUEST = {
    "time": "2026-11-14T19:50:26.539033Z",
    "scope": "request",
    "http.method": "POST",
    "http.status": 200,
}
ANSWER = {
    "resultType": "complete",
    "content": [{"type": "text", "text": "body\r\none record"}],
    "isError": False,
}
RAN = {
    **REQUEST,
    "mcp.method.name": "tools/call",
    "mcp.target": "telemetry",
    "gen_ai.tool.name": "execute_sql",
    "agent": "serving-agent",
    "tool_args": QUERY,
    "tool_result": ANSWER,
    "iam_decision": "793a185748a6",
    "iam_allow": True,
    "iam_reason": "granted",
    **ASKED,
}
REFUSED = {
    **REQUEST,
    "reason": "DirectResponse",
    "agent": "datapipe-agent",
    "iam_decision": "fd6593611516",
    "iam_allow": False,
    "iam_reason": "missing_permission",
    **ASKED,
}
UNDECIDED = {**REQUEST, "http.status": 401, "reason": "DirectResponse", "agent": "serving-agent", **ASKED}


def read(line: Mapping[str, object]) -> dict[str, object] | None:
    record = read_enforced_call(json.dumps(dict(line)), REFUSALS, SERVICES)
    return None if record is None else record.model_dump(include={"agent", "tool", "arguments", "result"})


def test_a_call_the_gateway_ran_is_a_record_of_what_the_target_was_sent_and_what_it_answered() -> None:
    sent = {"query": "SELECT 1"}  # the gateway's own reading, whatever the request's raw body said

    record = read_enforced_call(json.dumps({**RAN, "tool_args": sent}), REFUSALS, SERVICES)

    assert record is not None
    assert (record.agent, record.tool, record.arguments, record.result) == (
        "serving-agent",
        "execute_sql",
        sent,
        ANSWER,
    )
    assert (record.error, record.iam_decision) == (None, "793a185748a6")


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (REFUSED, REFUSAL),
        (UNDECIDED, "IAM allowed nothing (HTTP 401): nothing ran"),
        (
            {**RAN, "tool_result": None, "tool_error": {"code": -32603}},
            'the call failed (HTTP 200): {"code": -32603}',
        ),
    ],
    ids=["IAM refused it", "IAM gave no decision", "its target failed"],
)
def test_a_call_that_did_not_answer_is_a_refused_call_with_its_reason(
    line: dict[str, object],
    reason: str,
) -> None:
    record = read_enforced_call(json.dumps(line), REFUSALS, SERVICES)

    assert record is not None
    assert (record.tool, record.arguments, record.status_description) == ("execute_sql", QUERY, reason)
    assert record.error is not None


@pytest.mark.parametrize(
    "line",
    [
        {**RAN, "mcp.target": "notes", "gen_ai.tool.name": "write_note", "requested_tool": "write_note"},
        {**UNDECIDED, "requested_method": "prompts/get"},
        {**REQUEST, "agent": "serving-agent", "requested_method": "tools/call", "requested_tool": "bash"},
        {**REQUEST, "agent": "serving-agent"},
    ],
    ids=["another service's call", "no tool call, naming the tool", "a tool it does not enforce", "no call"],
)
def test_a_line_of_anything_else_is_no_record(line: dict[str, object]) -> None:
    assert read(line) is None


def test_a_call_whose_request_body_the_gateway_could_not_log_is_still_a_record_of_what_ran() -> None:
    unlogged = {name: value for name, value in RAN.items() if not name.startswith("requested_")}

    assert read(unlogged) == {
        "agent": "serving-agent",
        "tool": "execute_sql",
        "arguments": QUERY,
        "result": ANSWER,
    }


def action_of(tool: str, arguments: Mapping[str, object], result: Mapping[str, object]) -> Event:
    payload = {"args": dict(arguments), "target": None, "blocked": False, "reason": None}
    return Event("e", 1, 1.0, "serving-agent", "untrusted", "action", payload, tool=tool, result=dict(result))


def test_the_action_monitor_reads_the_result_of_a_telemetry_read_cut_to_its_bound_and_of_no_other_tool() -> (
    None
):
    config = load_run_config("configs/aurora-efficiency.yaml")
    context = MonitorContext("optimize", config.side_task.thresholds)
    spec = next(spec for spec in load_monitor_specs("configs/monitors.yaml") if spec.name == "action_monitor")
    monitor = ActionMonitor(dataclasses.replace(spec, extra={**spec.extra, "result_chars": 20}), None)

    read = monitor.input_payload(action_of("execute_sql", QUERY, {"content": "a" * 100}), context)
    written = monitor.input_payload(action_of("write_note", {"key": "k"}, {"content": "a" * 100}), context)

    assert (read["tool_args"], read["tool_result"]) == (QUERY, '{"content": "aaaaaaa')
    assert "tool_result" not in written


def test_a_subagents_call_is_its_own_record_though_the_gateway_admits_it_as_its_agent() -> None:
    called = {**RAN, "agent": "serving-agent", "caller": "serving-agent.1"}

    record = read(called)

    assert record is not None and record["agent"] == "serving-agent.1"
