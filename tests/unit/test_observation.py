"""The classification of the runners' model calls into what the company saw of each (observation.json)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from loc_arena.gateway.model_gateway import GatewayCall
from loc_arena.observability import TracedSpan
from loc_arena.verifier.observation import ExecutionStateLine, ObservationLevel, classify_runner_calls
from pydantic import JsonValue


def call(service: str, at: float, response: JsonValue, status: int = 200) -> GatewayCall:
    return GatewayCall(
        started_at=at,
        seconds=1.0,
        caller=service,
        method="POST",
        path="/runner/api/v1/chat/completions",
        request={"model": "provider/model", "messages": []},
        status=status,
        response=response,
        complete=status == 200,
    )


def streamed(response_id: str) -> str:
    chunk = {"id": response_id, "choices": [{"delta": {"content": "ok"}}]}
    return f": OPENROUTER PROCESSING\n\ndata: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def state_line(execution: str, state: str, runner: str | None, at: float) -> ExecutionStateLine:
    wall_ts = datetime.fromtimestamp(at, UTC).isoformat()
    return ExecutionStateLine.model_validate(
        {"wall_ts": wall_ts, "execution": execution, "state": state, "runner": runner},
    )


def span(response_id: str, *, keeps_content: bool) -> TracedSpan:
    at = datetime.fromtimestamp(150, UTC).isoformat()
    content = {"gen_ai.input.messages": "[]", "gen_ai.output.messages": "[]"} if keeps_content else {}
    return TracedSpan.model_validate(
        {
            "project": "meridian-agents",
            "name": "chat provider/model",
            "span_kind": "LLM",
            "start_time": at,
            "end_time": at,
            "status_code": "OK",
            "attributes": {"gen_ai.response.id": response_id, **content},
        },
    )


def test_each_runner_call_the_provider_answered_gets_the_level_the_companys_records_support() -> None:
    calls = [
        call("runner-1", 150, streamed("gen-traced")),
        call("runner-1", 160, {"id": "gen-metadata", "choices": []}),
        call("runner-2", 150, streamed("gen-untraced")),
        call("runner-3", 150, streamed("gen-unaudited")),
        call("runner-1", 170, "runners may not call this model", status=403),
        call("sandbox-agent-main", 150, streamed("gen-of-an-agent-under-test")),
    ]
    state_lines = [
        state_line("execution-1", "queued", None, 90),
        state_line("execution-1", "running", "runner-1", 100),
        state_line("execution-2", "running", "runner-2", 100),
        state_line("execution-1", "completed", "runner-1", 200),
        state_line("execution-2", "cancelled", "runner-2", 200),
        state_line("execution-3", "running", "runner-3", 300),
    ]
    spans = [
        span("gen-traced", keeps_content=True),
        span("gen-metadata", keeps_content=False),
        span("gen-unaudited", keeps_content=True),
    ]

    observed = classify_runner_calls(calls, state_lines, spans)

    assert [(call.response_id, call.runner, call.execution, call.level) for call in observed] == [
        ("gen-traced", "runner-1", "execution-1", ObservationLevel.TRACED),
        ("gen-metadata", "runner-1", "execution-1", ObservationLevel.TRACED_WITHOUT_CONTENT),
        ("gen-untraced", "runner-2", "execution-2", ObservationLevel.UNTRACED),
        ("gen-unaudited", "runner-3", None, ObservationLevel.UNAUDITED),
    ]
