"""Phoenix in a stack run: behind its guard, its spans exported when play ends, and the model calls they name.

A stand-in for compose answers whether Phoenix runs, whatever an agent left it in, and what the export printed
and how it ended. What the guard passes and refuses is checked against Phoenix itself in
``tests/integration/test_phoenix_guard_stack.py``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import AGENT_NETWORK, REPOSITORY, render_compose
from loc_arena.observability import (
    PHOENIX_GUARD_RULE,
    RESPONSE_ID_ATTRIBUTE,
    SPANS_FILE,
    SpansNotExportedError,
    export_spans,
    guard_phoenix,
    read_traced_response_ids,
)
from loc_arena.stack_play import play_phases
from pydantic import ValidationError

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


def test_a_line_without_attributes_is_no_span_phoenix_listed_and_stops_the_reading(tmp_path: Path) -> None:
    span = json.loads(span_line("a"))
    del span["attributes"]  # Phoenix's span list builds every span with its attributes, {} at least
    (tmp_path / SPANS_FILE).write_text(f"{json.dumps(span)}\n")

    with pytest.raises(ValidationError, match="attributes"):
        read_traced_response_ids(tmp_path)


def test_phoenix_moves_to_a_network_with_its_guard_alone_and_the_guard_takes_its_name_on_agent_net() -> None:
    compose = render_compose(AURORA, REPOSITORY, "aurora-efficiency", [])

    services = compose["services"]
    assert (
        services["phoenix"]["networks"],
        services["phoenix-guard"]["networks"],
        services["phoenix-guard"]["depends_on"],
        compose["networks"]["phoenix-net"],
    ) == (
        {"phoenix-net": {"aliases": ["phoenix-store"]}},
        {AGENT_NETWORK: {"aliases": ["phoenix"]}, "phoenix-net": {}},
        {"phoenix": {"condition": "service_healthy"}},
        {"internal": True},
    )


def test_the_guard_passes_what_its_rule_allows_to_phoenixs_port_its_config_escaped_for_compose() -> None:
    compose = render_compose(AURORA, REPOSITORY, "aurora-efficiency", [])

    flag, config = compose["services"]["phoenix-guard"]["command"]

    (bind,) = yaml.safe_load(config.replace("$$", "$"))["binds"]
    (route,) = bind["listeners"][0]["routes"]
    assert (flag, config.replace("$$", "").count("$"), bind["port"], route) == (
        "-c",
        0,
        6006,
        {
            "policies": {"authorization": {"rules": [{"allow": PHOENIX_GUARD_RULE}]}},
            "backends": [{"host": "phoenix-store:6006"}],
        },
    )


def test_a_scenario_with_no_phoenix_gets_no_guard() -> None:
    config = dataclasses.replace(
        AURORA,
        live_services=tuple(service for service in AURORA.live_services if service.name != "phoenix"),
    )
    document = render_compose(config, REPOSITORY, "aurora-efficiency", [])

    assert guard_phoenix(config, document) is document
