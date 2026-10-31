"""The export of Phoenix's spans out of a stack run when play ends, and the model calls the export names.

A stand-in for compose answers whether Phoenix runs, whatever an agent left it in, and what the export printed
and how it ended.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY
from loc_arena.observability import (
    RESPONSE_ID_ATTRIBUTE,
    SPANS_FILE,
    SpansNotExportedError,
    export_spans,
    read_traced_response_ids,
)
from loc_arena.stack_play import play_phases

AURORA = load_run_config(REPOSITORY / "configs" / "aurora-efficiency.yaml")  # its scenario runs Phoenix
COMPOSE = ["docker", "compose", "-p", "locarena-test"]


def span_line(span_id: str, attributes: object = None) -> bytes:
    span = {
        "project": "meridian-agents",
        "name": "chat provider/model",
        "context": {"trace_id": "0af7651916cd43dd8448eb211c80319c", "span_id": span_id},
        "span_kind": "LLM",
        "start_time": "2026-10-24T12:00:00+00:00",
        "end_time": "2026-10-24T12:00:01+00:00",
        "status_code": "OK",
        "attributes": attributes or {},
    }
    return f"{json.dumps(span)}\n".encode()


@dataclass
class StandInCompose:
    """Phoenix runs while ``running`` says, one answer per check; the export prints ``printed``, exits so."""

    running: list[bool]
    printed: bytes = b""
    returncode: int = 0
    commands: list[list[str]] = field(default_factory=list)

    def __call__(self, command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        if "ps" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                b"phoenix-container\n" if self.running.pop(0) else b"",
            )
        return subprocess.CompletedProcess(command, self.returncode, self.printed, b"the export's error")


def test_a_phoenix_not_running_exports_no_span(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    compose = StandInCompose(running=[False])

    with caplog.at_level(logging.WARNING):
        export_spans(compose, COMPOSE, AURORA, tmp_path)

    assert (len(compose.commands), (tmp_path / SPANS_FILE).exists(), "not running" in caplog.text) == (
        1,
        False,
        True,
    )


def test_a_phoenix_that_stops_during_the_export_keeps_the_spans_listed_whole(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listed_whole = span_line("b7ad6b7169203331")
    compose = StandInCompose(
        running=[True, False],
        printed=listed_whole + span_line("b7ad6b7169203332")[:40],
        returncode=137,
    )

    with caplog.at_level(logging.WARNING):
        export_spans(compose, COMPOSE, AURORA, tmp_path)

    exported = (tmp_path / SPANS_FILE).read_bytes()
    assert (exported, "the 1 spans it listed first are kept" in caplog.text) == (listed_whole, True)


def test_an_export_that_fails_while_phoenix_runs_raises(tmp_path: Path) -> None:
    compose = StandInCompose(running=[True, True], returncode=1)

    with pytest.raises(SpansNotExportedError, match="the export's error"):
        export_spans(compose, COMPOSE, AURORA, tmp_path)


def test_play_ends_with_the_runners_stopped_then_phoenixs_spans_exported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = StandInCompose(running=[True])
    monkeypatch.setattr(subprocess, "run", compose)

    play_phases(AURORA, COMPOSE, "honest", tmp_path, {}, {})

    after_play = [command[len(COMPOSE) :] for command in compose.commands[1:]]
    assert [command[:4] for command in after_play] == [
        ["stop", "runner-1", "runner-2", "runner-3"],
        ["ps", "--status", "running", "--quiet"],
        ["exec", "-T", "phoenix", "python3.13"],
    ]


def test_the_spans_name_the_replies_they_trace_and_one_pydantic_cannot_read_is_left_out(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    nested: object = "deep"
    for _ in range(300):  # past the nesting pydantic reads
        nested = [nested]
    lines = [
        span_line("a", {RESPONSE_ID_ATTRIBUTE: "gen-1"}),
        span_line("b", {RESPONSE_ID_ATTRIBUTE: ["gen-2"]}),  # an agent's span, naming no reply
        span_line("c", {RESPONSE_ID_ATTRIBUTE: "gen-3", "padding": nested}),
        span_line("d", {"gen_ai.tool.name": "bash"}),
    ]
    (tmp_path / SPANS_FILE).write_bytes(b"".join(lines))

    with caplog.at_level(logging.WARNING):
        traced = read_traced_response_ids(tmp_path)

    assert (traced, "Left out a span" in caplog.text) == (frozenset({"gen-1"}), True)


def test_no_export_names_no_reply(tmp_path: Path) -> None:
    assert read_traced_response_ids(tmp_path) == frozenset()
