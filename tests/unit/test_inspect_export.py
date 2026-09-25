from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import EvalConfig, EvalDataset, EvalSample, EvalSpec, read_eval_log
from loc_arena.config import load_run_config
from loc_arena.logging_ import inspect_export
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.logging_.inspect_export import EpisodeExport, write_run_eval

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
