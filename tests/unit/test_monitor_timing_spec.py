"""A monitor's declared timing decides which thread scores it; the loader reads its aggregation settings."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from loc_arena.monitors.registry import (
    MonitorModelCaller,
    load_monitor_specs,
    load_monitors,
    run_monitors_split,
)
from loc_arena.protocol import monitor_context

from tests.unit._monitor_support import (
    ACTION_EVENT,
    RUN_CONFIG,
    VERDICT_REPLY,
    load_one_monitor_spec,
    write_monitors_file,
    write_prompt_template,
)

# How long the split driver waits for an async monitor; ample, since the stub model replies at once.
ASYNC_JOIN_DEADLINE_SECONDS = 20.0


def _live_action_monitor_file(directory: Path, **entry: object) -> Path:
    """A monitors.yaml with one live action_monitor, so scoring an action calls the model caller."""
    template = write_prompt_template(directory, "Rate this step.\n{inputs}")
    return write_monitors_file(directory, mode="live", prompt_ref=template, **entry)


def _thread_recording_caller(threads: list[threading.Thread]) -> MonitorModelCaller:
    """A model caller that records the thread it runs on and replies with a parseable verdict."""

    def call(monitor: str, prompt: str, temperature: float) -> str:
        threads.append(threading.current_thread())
        return VERDICT_REPLY

    return call


def test_a_monitor_that_declares_no_timing_is_scored_on_the_calling_thread(tmp_path: Path) -> None:
    registry = load_monitors(_live_action_monitor_file(tmp_path), RUN_CONFIG.models)
    scoring_threads: list[threading.Thread] = []
    context = monitor_context(RUN_CONFIG, model_caller=_thread_recording_caller(scoring_threads))
    calling_thread = threading.current_thread()

    run_monitors_split(registry, [ACTION_EVENT], context, async_default_timeout_s=ASYNC_JOIN_DEADLINE_SECONDS)

    assert [thread is calling_thread for thread in scoring_threads] == [True]


def test_a_monitor_declared_async_is_scored_off_the_calling_thread(tmp_path: Path) -> None:
    registry = load_monitors(_live_action_monitor_file(tmp_path, timing="async"), RUN_CONFIG.models)
    scoring_threads: list[threading.Thread] = []
    context = monitor_context(RUN_CONFIG, model_caller=_thread_recording_caller(scoring_threads))
    calling_thread = threading.current_thread()

    run_monitors_split(registry, [ACTION_EVENT], context, async_default_timeout_s=ASYNC_JOIN_DEADLINE_SECONDS)

    assert [thread is calling_thread for thread in scoring_threads] == [False]


def test_a_timing_other_than_sync_or_async_is_rejected_at_load(tmp_path: Path) -> None:
    monitors = write_monitors_file(tmp_path, timing="online")

    with pytest.raises(ValueError, match="timing must be 'sync' or 'async'"):
        load_monitor_specs(monitors)


def test_the_loader_reads_the_aggregation_settings_a_monitor_declares(tmp_path: Path) -> None:
    spec = load_one_monitor_spec(tmp_path, per_monitor_reduce="mean", weight=0.0, timeout_s=7)

    assert (spec.per_monitor_reduce, spec.weight, spec.timeout_s) == ("mean", 0.0, 7.0)
