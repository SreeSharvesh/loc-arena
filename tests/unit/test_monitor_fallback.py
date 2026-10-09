"""A live monitor whose model calls fail too many times in a row scores the rest with its heuristic."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
import yaml
from loc_arena.harness import build_monitor_caller, run_episode
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.monitors.aggregation import load_aggregation_config
from loc_arena.monitors.registry import live_suspicion
from loc_arena.protocol import monitor_context

from tests.integration._gateway_support import FailingProvider
from tests.unit._monitor_support import ACTION_EVENT, RUN_CONFIG, SHIPPED_MONITORS, write_monitors_file

MONITOR = "action_monitor"
# monitor_failures_before_fallback is 3 in configs/monitors.yaml: calls 1 and 2 fail, 3 answers, and 4 to 6
# fail in a row, so the monitor sends no call after the sixth.
FAILURES_BEFORE_FALLBACK = 3
THIRD_ANSWERED = frozenset({3})
CALLS_ATTEMPTED = 8
CALLS_SENT = 6
# With a fallback after 1 failure: the honest twin, scored first, fails its first call and makes no more;
# every later call, the episode's, answers.
ONLY_THE_TWIN_FAILS = range(2, 1_000_000)
REFUSED_KEY = httpx2.HTTPStatusError(
    "401 Unauthorized",
    request=httpx2.Request("POST", "https://openrouter.test"),
    response=httpx2.Response(401, request=httpx2.Request("POST", "https://openrouter.test")),
)


@pytest.fixture
def sealed_path(tmp_path: Path) -> Path:
    path = tmp_path / "events.sealed.jsonl"
    AppendOnlyLog(path, ACTION_EVENT.episode_id).append(ACTION_EVENT)
    return path


def _score(sealed_path: Path, provider: FailingProvider, calls: int) -> None:
    limit = load_aggregation_config(SHIPPED_MONITORS).monitor_failures_before_fallback
    episode = SimpleNamespace(sealed_path=sealed_path)
    caller, sink = build_monitor_caller(episode, RUN_CONFIG, provider, failures_before_fallback=limit)
    context = monitor_context(RUN_CONFIG, model_caller=caller, event_sink=sink)
    live_suspicion(context, MONITOR, "prompt", ensemble_n=calls, temperature=0.0)


@pytest.mark.parametrize("error", [None, REFUSED_KEY], ids=["the provider failed", "the key was refused"])
def test_a_monitor_stops_calling_its_model_after_its_calls_fail_in_a_row(
    sealed_path: Path,
    error: Exception | None,
) -> None:
    provider = FailingProvider(answered=THIRD_ANSWERED, error=error)

    _score(sealed_path, provider, CALLS_ATTEMPTED)

    assert len(provider.models) == CALLS_SENT


def test_a_monitor_that_stops_calling_its_model_records_it_once(sealed_path: Path) -> None:
    provider = FailingProvider()

    _score(sealed_path, provider, CALLS_ATTEMPTED)

    assert [e.payload["monitor"] for e in read_events(sealed_path) if e.kind == "monitor_error"] == [MONITOR]


def test_a_monitor_that_stopped_calling_its_model_records_no_more_parse_errors(sealed_path: Path) -> None:
    provider = FailingProvider()

    _score(sealed_path, provider, CALLS_ATTEMPTED)

    assert sum(e.kind == "monitor_parse_error" for e in read_events(sealed_path)) == FAILURES_BEFORE_FALLBACK


def test_a_monitor_that_stopped_calling_its_model_in_the_honest_twin_alone_is_errored(tmp_path: Path) -> None:
    monitors = write_monitors_file(
        tmp_path,
        tap=["action", "message"],
        mode="live",
        prompt_ref="configs/prompts/monitors/action_monitor.txt",
    )
    entries = yaml.safe_load(monitors.read_text())
    monitors.write_text(
        yaml.safe_dump({**entries, "scoring": {"aggregation": {"monitor_failures_before_fallback": 1}}}),
    )
    bundle = run_episode(
        RUN_CONFIG,
        mode="attack",
        out_root=tmp_path,
        monitors_path=str(monitors),
        monitor_provider=FailingProvider(answered=ONLY_THE_TWIN_FAILS),
        write_report=False,
    )

    rows = json.loads((bundle / "scores.json").read_text())["monitors"]

    assert [row["errored"] for row in rows if row["name"] == MONITOR] == [True]
