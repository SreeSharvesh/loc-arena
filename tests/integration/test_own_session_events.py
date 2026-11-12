"""A play whose agents ran their own sessions gets its events from the two records: model calls and tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from loc_arena.recorded_events import build_recorded_events
from loc_arena.workspace_service import WORKSPACE_SERVICE

from tests.integration._records_support import (
    LIVE,
    MIRROR,
    SEALED,
    events_of,
    play,
    read_unattributed_reasons,
    read_window,
    write_session_calls,
)

AGENT = "serving-agent"


@pytest.fixture
def run(tmp_path: Path) -> Path:
    """A played run with two model calls of serving-agent's loop and, between them, one bash call."""
    run = play(tmp_path / "run")
    start = read_window(run / "episode")[0].timestamp()
    write_session_calls(run, AGENT, start + 0.001, start + 0.003, tools=["bash"])
    bash = {
        "time": datetime.fromtimestamp(start + 0.002, UTC).isoformat(),
        "gen_ai.tool.name": "bash",
        "gen_ai.tool.call.arguments": {"command": "ls"},
        "gen_ai.tool.call.result": {"returncode": 0, "stdout": "a\n", "stderr": ""},
        "gen_ai.agent.name": AGENT,
    }
    records = run / "episode" / "records" / f"{WORKSPACE_SERVICE}.jsonl"
    records.parent.mkdir()
    records.write_text(json.dumps(bash) + "\n")
    return run


def test_each_model_call_of_a_loop_is_its_agents_turn_on_both_logs(run: Path) -> None:
    build_recorded_events(run, LIVE, {"episode": None})

    turns = [
        [
            (e.turn, e.actor_role, e.payload["turn_id"])
            for e in events_of(run / "episode", log, "inference_call", AGENT)
        ]
        for log in (SEALED, MIRROR)
    ]
    expected = [(1, "untrusted", f"{AGENT}:t1"), (2, "untrusted", f"{AGENT}:t2")]
    assert turns == [expected, expected]


def test_a_workspace_call_is_an_action_in_the_turn_that_made_it(run: Path) -> None:
    build_recorded_events(run, LIVE, {"episode": None})

    (action,) = events_of(run / "episode", MIRROR, "action", AGENT)
    assert (action.tool, action.turn, action.payload["args"], action.result) == (
        "bash",
        1,
        {"command": "ls"},
        {"returncode": 0, "stdout": "a\n", "stderr": ""},
    )


def test_a_request_that_offered_a_local_tool_is_reported(run: Path) -> None:
    build_recorded_events(run, LIVE, {"episode": None})

    assert f"a model request of {AGENT}'s loop offered tools it runs itself" in read_unattributed_reasons(run)
