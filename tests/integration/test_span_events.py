"""The spans Phoenix held of traced executions become events the monitors read, built after play.

A stack run exports Phoenix's spans to ``services/phoenix.spans.jsonl``, beside the agents service's log that
records each run; a test writes both for a run played in process, as it does the services' logs in
``test_recorded_events``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loc_arena.harness import build_recorded_run_events
from loc_arena.recorded_events import SPAN_TOOL

from tests.integration.test_recorded_events import (
    LIVE,
    LOOK_AROUND,
    MIRROR,
    SEALED,
    events_of,
    play,
    read_unattributed_reasons,
    read_window,
)


def span_line(at: datetime, attributes: dict[str, str]) -> str:
    """A model call's span as Phoenix's REST API lists it, started and ended at ``at``."""
    return json.dumps(
        {
            "project": "meridian-agents",
            "id": "U3Bhbjox",
            "name": "chat provider/model",
            "context": {"trace_id": "0af7651916cd43dd8448eb211c80319c", "span_id": "b7ad6b7169203331"},
            "span_kind": "LLM",
            "parent_id": None,
            "start_time": at.isoformat(),
            "end_time": at.isoformat(),
            "status_code": "OK",
            "status_message": "",
            "attributes": attributes,
            "events": [],
        },
    )


def test_a_span_is_an_action_of_the_agent_the_agents_service_recorded_starting_its_run_and_no_other(
    tmp_path: Path,
) -> None:
    run = play(tmp_path / "run", episode=[LOOK_AROUND])
    started, _ = read_window(run / "episode")
    run_of = {"meridian.execution.id": "execution-1", "meridian.runner": "runner-1"}
    traced = {**run_of, "meridian.execution.started_by": "serving-agent", "gen_ai.response.id": "gen-1"}
    forged = {**run_of, "meridian.execution.started_by": "agent-main", "gen_ai.response.id": "gen-2"}
    (run / "services").mkdir()
    running = {"wall_ts": started.isoformat(), "execution": "execution-1", "state": "running"}
    audit_line = {**running, "runner": "runner-1", "started_by": "serving-agent"}
    (run / "services" / "agents.log").write_text(f"{json.dumps(audit_line)}\n")
    spans = [
        span_line(started, traced),
        span_line(started, forged),
        span_line(started, {"gen_ai.response.id": "3"}),
    ]
    (run / "services" / "phoenix.spans.jsonl").write_text("".join(f"{line}\n" for line in spans))

    build_recorded_run_events(LIVE, run)

    built = {
        log: [
            (event.target_id, event.payload["args"]["kind"], event.payload["args"]["gen_ai.response.id"])
            for event in events_of(run / "episode", log, "action", "serving-agent")
            if event.tool == SPAN_TOOL
        ]
        for log in (SEALED, MIRROR)
    }
    assert built == {log: [("execution-1", "LLM", "gen-1")] for log in (SEALED, MIRROR)}
    # The traced span is built in turn 0, outside serving-agent's turns. The forged one claims a run the
    # agents service never recorded, agent-main's, and the last names none: neither is any agent's.
    assert read_unattributed_reasons(run) == [
        "no covering turn",
        "not an agent of the run",
        "not an agent of the run",
    ]
