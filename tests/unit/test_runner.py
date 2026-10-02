"""The runner's monitoring of a finished episode: a failed monitor call is an errored verdict, not a crash."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayCallError
from loc_arena.harness import apply_mode, load_protocol, run_episode
from loc_arena.logging_.events import read_events
from loc_arena.runner import EpisodeMonitoring, MonitorModelRoute
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.task import assemble_scripted_episode

from tests.integration._gateway_support import FailingProvider
from tests.unit._golden import GOLDEN
from tests.unit._stack_services import open_in_process

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MONITORS = "configs/monitors.yaml"  # every model-backed monitor in it is live


def test_a_failed_monitor_call_is_recorded_as_that_monitors_error(tmp_path: Path) -> None:
    wiring = open_in_process(tmp_path, CONFIG, monitor_provider=FailingProvider())
    route = MonitorModelRoute(wiring, SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts))

    with pytest.raises(GatewayCallError):
        route.call_model("action_monitor", "score this", 0.0)

    (error,) = read_events(tmp_path / "sealed.jsonl")
    assert (error.kind, error.payload["monitor"]) == ("monitor_error", "action_monitor")


def test_an_episode_whose_monitor_calls_all_fail_is_scored_by_the_monitors_heuristics(tmp_path: Path) -> None:
    config = apply_mode(CONFIG, "attack")
    monitoring = EpisodeMonitoring(load_protocol(config, MONITORS), calls_models=True)

    episode = assemble_scripted_episode(
        config,
        tmp_path,
        robust=True,
        monitoring=monitoring,
        monitor_provider=FailingProvider(),
    )

    heuristic = json.loads((GOLDEN / "attack" / "scores.json").read_text())["monitor_verdicts"]
    assert [dataclasses.asdict(verdict) for verdict in episode.verdicts] == [
        {**verdict, "from_model": False} for verdict in heuristic
    ]


def test_a_monitor_whose_calls_failed_is_reported_errored(tmp_path: Path) -> None:
    bundle = run_episode(
        CONFIG,
        mode="honest",
        out_root=tmp_path,
        monitors_path=MONITORS,
        monitor_provider=FailingProvider(),
        write_report=False,
    )

    rows = json.loads((bundle / "scores.json").read_text())["monitors"]
    assert {row["name"]: row["errored"] for row in rows}["action_monitor"] is True
