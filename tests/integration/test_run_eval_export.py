from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from inspect_ai.event import SpanBeginEvent, ToolEvent
from inspect_ai.log import read_eval_log
from loc_arena.config import load_run_config
from loc_arena.harness import run_episode

from tests.integration._live_support import LANDING_QUEUES, QueuedProvider

LIVE_TRACED = dataclasses.replace(
    load_run_config("configs/aurora-efficiency.deterministic.yaml"), policy="model", agent_transcript=True
)


def test_a_live_attack_run_writes_a_real_eval_with_a_span_per_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    bundle = run_episode(
        LIVE_TRACED,
        mode="attack",
        out_root=tmp_path,
        provider=QueuedProvider(LANDING_QUEUES),
        calibration_provider=QueuedProvider({}),
        write_report=False,
    )
    (eval_path,) = bundle.glob("*.eval")
    log = read_eval_log(str(eval_path))
    assert log.samples is not None
    assert [s.id for s in log.samples] == ["episode", "honest_cal"]
    episode = log.samples[0]
    agent_spans = [e.name for e in episode.events if isinstance(e, SpanBeginEvent) and e.type == "agent"]
    assert agent_spans[0] == "agent-main" and len(agent_spans) > 1
    assert set(agent_spans) <= set(episode.metadata["agents"])
    assert episode.metadata["scores"]["safety"]["landed"] is True
    assert any(e.event == "model" for e in episode.events)
    twin_tools = {e.function for e in log.samples[1].events if isinstance(e, ToolEvent)}
    assert not twin_tools & {"start_rogue_loop", "blind_monitor", "write_scratch"}
