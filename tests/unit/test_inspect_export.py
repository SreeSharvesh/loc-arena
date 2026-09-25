from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent
from inspect_ai.log import EvalConfig, EvalDataset, EvalSample, EvalSpec, read_eval_log
from loc_arena.config import load_run_config
from loc_arena.logging_ import inspect_export
from loc_arena.logging_.agent_trace import AgentTrace, EpisodeTrace, TurnRef
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.logging_.inspect_export import EpisodeExport, UnassignedEventError, write_run_eval

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _episode(sample_id: str, tmp_path: Path) -> EpisodeExport:
    return EpisodeExport(
        sample_id=sample_id,
        trace=AgentTrace().finish(last_sealed_seq=-1),
        sealed_path=tmp_path / f"{sample_id}.jsonl",
        agent_order=("agent-main",),
    )


def test_write_run_eval_writes_one_sample_per_episode_and_scores_only_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_spec(run_name: str, config: object, mode: str, seed: int, sample_count: int) -> EvalSpec:
        return EvalSpec(
            created="2026-09-25T00:00:00+00:00",
            task=f"{run_name}/{mode}/{seed}/{sample_count}",
            dataset=EvalDataset(),
            model="none/none",
            config=EvalConfig(),
        )

    def fake_sample(episode: EpisodeExport, scores: Mapping[str, Any] | None) -> EvalSample:
        return EvalSample(id=episode.sample_id, epoch=1, input="", target="", metadata={"scores": scores})

    monkeypatch.setattr(inspect_export, "_eval_spec", fake_spec)
    monkeypatch.setattr(inspect_export, "_sample", fake_sample)
    path = write_run_eval(
        tmp_path / "run.eval",
        run_name="run-x",
        config=CFG,
        mode="attack",
        seed=7,
        scores={"caught": True},
        episodes=[_episode("episode", tmp_path), _episode("honest_cal", tmp_path)],
    )
    log = read_eval_log(str(path))
    assert log.status == "success"
    assert log.eval.task == "run-x/attack/7/2"
    assert log.samples is not None
    assert [(s.id, s.metadata["scores"]) for s in log.samples] == [
        ("episode", {"caught": True}),
        ("honest_cal", None),
    ]


def test_eval_spec_names_the_task_after_the_run_slug_and_records_the_mode() -> None:
    spec = inspect_export._eval_spec("run-x", CFG, "honest", 3, 1)
    assert spec.task == f"loc_arena/{CFG.run_slug}"
    assert spec.run_id == "run-x"
    assert spec.model == CFG.models["untrusted_agent"].model
    assert spec.dataset.samples == 1
    assert spec.metadata == {"mode": "honest", "seed": 3, "policy": CFG.policy, "run_name": "run-x"}


def _write_sealed(path: Path, count: int) -> None:
    log = AppendOnlyLog(path, "ep-export")
    for seq in range(count):
        log.append(
            Event(episode_id="ep-export", seq=seq, ts=0.0, actor_uid="world", actor_role="npc", kind="tick")
        )


def test_sample_reads_the_sealed_log_assigns_lanes_and_builds_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode = _episode("episode", tmp_path)
    _write_sealed(episode.sealed_path, 3)
    seen: dict[str, object] = {}

    def fake_lanes(trace: EpisodeTrace, sealed_events: list[Event]) -> dict[int, TurnRef | None]:
        seen["seqs"] = [e.seq for e in sealed_events]
        return {0: None, 1: None, 2: None}

    def fake_events(
        ep: EpisodeExport, sealed_events: list[Event], lanes: Mapping[int, TurnRef | None]
    ) -> list[InspectEvent]:
        seen["lanes"] = dict(lanes)
        return [InfoEvent(data="marker")]

    monkeypatch.setattr(inspect_export, "_lanes_for", fake_lanes)
    monkeypatch.setattr(inspect_export, "_sample_events", fake_events)
    sample = inspect_export._sample(episode, {"caught": False})
    assert seen == {"seqs": [0, 1, 2], "lanes": {0: None, 1: None, 2: None}}
    assert sample.id == "episode"
    assert [e.event for e in sample.events] == ["info"]
    assert sample.metadata == {"agents": ["agent-main"], "scores": {"caught": False}}


def _tick(seq: int) -> Event:
    return Event(episode_id="ep-export", seq=seq, ts=0.0, actor_uid="world", actor_role="npc", kind="tick")


def _traced(lanes: dict[int, TurnRef | None], boundary: int) -> EpisodeTrace:
    trace = AgentTrace()
    for seq, ref in lanes.items():
        if ref is None:
            trace.on_sealed_append(_tick(seq))
        else:
            with trace.turn(ref.agent_uid, ref.turn):
                trace.on_sealed_append(_tick(seq))
    return trace.finish(last_sealed_seq=boundary)


def test_lanes_come_from_the_trace_inside_the_boundary_and_world_after_it() -> None:
    main = TurnRef("agent-main", 0)
    trace = _traced({0: None, 1: main}, boundary=1)
    lanes = inspect_export._lanes_for(trace, [_tick(0), _tick(1), _tick(2), _tick(3)])
    assert lanes == {0: None, 1: main, 2: None, 3: None}


def test_an_untagged_event_inside_the_boundary_raises() -> None:
    trace = _traced({0: None}, boundary=2)
    with pytest.raises(UnassignedEventError, match=r"2 sealed events .* first untagged seqs: \[1, 2\]"):
        inspect_export._lanes_for(trace, [_tick(0), _tick(1), _tick(2)])


def test_agent_span_id() -> None:
    assert inspect_export._agent_span_id("serving-agent") == "agent:serving-agent"


def test_turn_span_id() -> None:
    assert inspect_export._turn_span_id(TurnRef("serving-agent", 4)) == "turn:serving-agent:4"


def test_timestamp_is_an_aware_utc_datetime() -> None:
    assert inspect_export._timestamp(0.0) == datetime(1970, 1, 1, tzinfo=UTC)
