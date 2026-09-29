"""The runner's monitoring of a finished episode: a failed monitor call is an errored verdict, not a crash."""

from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayCallError
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.harness import apply_mode, load_protocol, run_episode
from loc_arena.logging_.events import read_events
from loc_arena.runner import EpisodeMonitoring, MonitorModelRoute
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.task import assemble_scripted_episode

from tests.unit._golden import GOLDEN
from tests.unit._stack_services import open_in_process

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MONITORS = "configs/monitors.yaml"  # every model-backed monitor in it is live
FAILURES_BEFORE_FALLBACK = 3
EVENTS = 10  # more events to score than failed calls before the fallback


class FailingProvider:
    """The provider boundary, failing every call as OpenRouter does once its retries are spent."""

    def __init__(self) -> None:
        """Count the calls it fails."""
        self.calls = 0

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.calls += 1
        raise ProviderError(f"{model}: the provider kept answering 503")


class EverySecondCallFailingProvider(FailingProvider):
    """The provider boundary on a flaky day: every second call fails, the others answer a verdict."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        if self.calls % 2 == 0:
            return super().generate(model, messages, temperature, max_tokens, tools)
        self.calls += 1
        return ProviderResult('{"suspicion": 0.1, "reason": "routine"}', prompt_tokens=1, completion_tokens=1)


def _monitor_route(tmp_path: Path, monitor_provider: FailingProvider) -> MonitorModelRoute:
    wiring = open_in_process(tmp_path, CONFIG, monitor_provider=monitor_provider)
    return MonitorModelRoute(
        wiring,
        SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts),
        failures_before_fallback=FAILURES_BEFORE_FALLBACK,
    )


def _call_model(route: MonitorModelRoute, times: int) -> None:
    for _ in range(times):
        with contextlib.suppress(GatewayCallError):
            route.call_model("action_monitor", "score this", 0.0)


def test_a_failed_monitor_call_is_recorded_as_that_monitors_error(tmp_path: Path) -> None:
    wiring = open_in_process(tmp_path, CONFIG, monitor_provider=FailingProvider())
    route = MonitorModelRoute(
        wiring,
        SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts),
        failures_before_fallback=FAILURES_BEFORE_FALLBACK,
    )

    with pytest.raises(GatewayCallError):
        route.call_model("action_monitor", "score this", 0.0)

    (error,) = [event for event in read_events(tmp_path / "sealed.jsonl") if event.kind == "monitor_error"]
    assert error.payload["monitor"] == "action_monitor"


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


def test_a_monitor_calls_its_model_no_more_after_the_configured_failures_in_a_row(tmp_path: Path) -> None:
    provider = FailingProvider()
    route = _monitor_route(tmp_path, provider)

    _call_model(route, EVENTS)

    assert provider.calls == FAILURES_BEFORE_FALLBACK


def test_a_monitor_keeps_calling_its_model_while_its_failures_are_not_in_a_row(tmp_path: Path) -> None:
    provider = EverySecondCallFailingProvider()
    route = _monitor_route(tmp_path, provider)

    _call_model(route, EVENTS)

    assert provider.calls == EVENTS
