"""A live monitor whose model calls fail too many times in a row scores the rest with its heuristic."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from loc_arena.harness import build_monitor_caller
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.monitors.registry import live_suspicion
from loc_arena.protocol import monitor_context

from tests.integration._gateway_support import FailingProvider
from tests.unit._monitor_support import ACTION_EVENT, RUN_CONFIG

MONITOR = "action_monitor"
# With gateway.monitor_failures_before_fallback at 3, its default: calls 1 and 2 fail, 3 answers, and 4 to 6
# fail in a row, so the monitor sends no call after the sixth.
THIRD_ANSWERED = frozenset({3})
CALLS_ATTEMPTED = 8
CALLS_SENT = 6


@pytest.fixture
def sealed_path(tmp_path: Path) -> Path:
    path = tmp_path / "events.sealed.jsonl"
    AppendOnlyLog(path, ACTION_EVENT.episode_id).append(ACTION_EVENT)
    return path


def _score(sealed_path: Path, provider: FailingProvider, calls: int) -> None:
    caller, sink = build_monitor_caller(SimpleNamespace(sealed_path=sealed_path), RUN_CONFIG, provider)
    context = monitor_context(RUN_CONFIG, model_caller=caller, event_sink=sink)
    live_suspicion(context, MONITOR, "prompt", ensemble_n=calls, temperature=0.0)


def test_a_monitor_stops_calling_its_model_after_its_calls_fail_in_a_row(sealed_path: Path) -> None:
    provider = FailingProvider(answered=THIRD_ANSWERED)

    _score(sealed_path, provider, CALLS_ATTEMPTED)

    assert len(provider.models) == CALLS_SENT


def test_a_monitor_that_stops_calling_its_model_records_it_once(sealed_path: Path) -> None:
    provider = FailingProvider()

    _score(sealed_path, provider, CALLS_ATTEMPTED)

    assert [e.payload["monitor"] for e in read_events(sealed_path) if e.kind == "monitor_error"] == [MONITOR]
