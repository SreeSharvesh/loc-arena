from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.run_watcher import Step, read_steps, watch_run


def _write_session(run: Path, phase: str, agent: str, *lines: dict[str, object]) -> None:
    home = run / phase / "loops" / agent / ".pi"
    home.mkdir(parents=True)
    (home / "session.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))


def _say(timestamp: str, text: str) -> dict[str, object]:
    message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return {"type": "message", "timestamp": timestamp, "message": message}


def test_an_assistant_line_gives_its_thought_its_words_and_its_tool_call() -> None:
    content = [
        {"type": "thinking", "thinking": "Read the sprint\nfirst.", "thinkingSignature": "s"},
        {"type": "text", "text": "I'll read the tickets."},
        {"type": "toolCall", "id": "call_1", "name": "search_tickets", "arguments": {"project": "AURORA"}},
    ]
    line = json.dumps({"type": "message", "message": {"role": "assistant", "content": content}})

    steps = read_steps(line, width=400)

    assert steps == [
        Step("THINK", "Read the sprint first."),
        Step("SAY", "I'll read the tickets."),
        Step("CALL", 'search_tickets {"project": "AURORA"}'),
    ]


@pytest.mark.parametrize(
    "line",
    [
        {"type": "session", "version": 3, "cwd": "/workspace"},
        {"type": "message", "message": {"role": "system", "content": "You are agent-main."}},
    ],
    ids=["the session's header", "the system prompt"],
)
def test_a_line_that_is_no_step_of_the_agents_gives_none(line: dict[str, object]) -> None:
    assert read_steps(json.dumps(line), width=400) == []


def test_a_step_longer_than_the_width_is_cut_to_it() -> None:
    line = json.dumps({"type": "message", "message": {"role": "toolResult", "content": "0123456789"}})

    assert read_steps(line, width=4) == [Step("RESULT", "0123")]


def test_a_finished_run_prints_each_step_with_its_play_and_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_session(tmp_path, "episode", "agent-main", _say("2026-11-12T19:35:09.000Z", "Kickoff is up."))
    _write_session(tmp_path, "honest_cal", "eval-agent", _say("2026-11-12T19:52:01.000Z", "Tests are green."))

    watch_run(tmp_path, agent=None, width=400, poll_seconds=0)

    assert capsys.readouterr().out.splitlines() == [
        "19:35:09 attack agent-main           SAY    Kickoff is up.",
        "19:52:01 twin   eval-agent           SAY    Tests are green.",
    ]


def test_naming_an_agent_prints_its_steps_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_session(tmp_path, "episode", "agent-main", _say("2026-11-12T19:35:09.000Z", "Kickoff is up."))
    _write_session(tmp_path, "episode", "eval-agent", _say("2026-11-12T19:35:10.000Z", "Reading my ticket."))

    watch_run(tmp_path, agent="eval-agent", width=400, poll_seconds=0)

    assert capsys.readouterr().out.splitlines() == [
        "19:35:10 attack eval-agent           SAY    Reading my ticket.",
    ]
