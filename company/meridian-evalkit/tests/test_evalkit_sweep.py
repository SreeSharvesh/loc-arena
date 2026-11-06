"""Tests for the hyperparameter sweep: the learning curve, the run loop, early stopping, and the job driver.

Expected values are worked out by hand from the curve definition (a ``rate``-fraction of the remaining gap
per step), never recomputed the way the code does, so a change in behaviour fails the test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from meridian_common.jobclient import JobClient
from meridian_evalkit.errors import SweepError
from meridian_evalkit.sweep import (
    KeepAllPruner,
    SweepSpec,
    TrialConfig,
    build_sweep,
    pick_winner,
    run_sweep,
)
from meridian_evalkit.sweep.__main__ import run_trial
from meridian_evalkit.sweep.driver import submit_sweep
from meridian_evalkit.sweep.trial import TrialState, advance, initial_state, running_metric


class _KeepTop:
    """A test pruner: keep all trials until ``warmup`` steps have run, then keep the top ``k`` by value."""

    def __init__(self, warmup: int, k: int) -> None:
        self._warmup, self._k = warmup, k

    def survivors(self, states: Mapping[str, TrialState], step: int, max_steps: int) -> frozenset[str]:
        alive = [tid for tid, state in states.items() if state.step < max_steps]
        if step + 1 < self._warmup:
            return frozenset(alive)
        ranked = sorted(alive, key=lambda tid: (-states[tid].value, tid))
        return frozenset(ranked[: self._k])


class _FakeJobsvc:
    """A minimal job transport that records every submitted spec and hands back incrementing ids."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(spec)
        return {"job_id": f"job-{len(self.submitted)}"}

    def status(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "state": "running"}

    def logs(self, job_id: str) -> list[str]:
        return []

    def cancel(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "state": "cancelled"}


def test_advance_closes_half_the_gap_at_rate_500() -> None:
    config = TrialConfig("t", target=100, rate=500)
    assert running_metric(advance(initial_state(config), config)) == 50


def test_advance_is_monotone_and_saturates_at_the_target() -> None:
    config = TrialConfig("t", target=100, rate=500)
    state = initial_state(config)
    values = []
    for _ in range(20):
        state = advance(state, config)
        values.append(state.value)
    assert values == sorted(values)  # never decreases
    assert max(values) == 100  # reaches, never exceeds, the ceiling
    assert state.step == 20


def test_advance_rejects_a_config_for_a_different_trial() -> None:
    state = initial_state(TrialConfig("a", target=10, rate=500))
    with pytest.raises(SweepError):
        advance(state, TrialConfig("b", target=10, rate=500))


@pytest.mark.parametrize("target, rate", [(-1, 500), (10, 0), (10, 1001)])
def test_trial_config_rejects_out_of_range_values(target: int, rate: int) -> None:
    with pytest.raises(SweepError):
        TrialConfig("t", target=target, rate=rate)


def test_baseline_runs_every_trial_to_completion_and_picks_the_highest_ceiling() -> None:
    spec = SweepSpec(
        trials=(
            TrialConfig("hi", target=90, rate=500),
            TrialConfig("mid", target=60, rate=500),
            TrialConfig("lo", target=30, rate=500),
        ),
        max_steps=5,
    )
    result = run_sweep(spec, KeepAllPruner())
    assert result.steps_run == 15  # 3 trials * 5 steps, nothing stopped
    assert result.winner == "hi"
    assert all(state.step == 5 for state in result.final.values())


def test_early_stopping_cuts_cost_while_keeping_the_winner() -> None:
    spec = SweepSpec(
        trials=(
            TrialConfig("fast_low", target=60, rate=900),
            TrialConfig("slow_high", target=100, rate=150),
            TrialConfig("loser", target=20, rate=900),
        ),
        max_steps=20,
    )
    baseline = run_sweep(spec, KeepAllPruner())
    pruned = run_sweep(spec, _KeepTop(warmup=3, k=2))
    assert baseline.winner == "slow_high"  # the late-emerging high ceiling wins when every trial completes
    assert pruned.winner == "slow_high"  # a rule that drops only the clear loser keeps the winner
    assert pruned.steps_run < baseline.steps_run


def test_over_eager_pruning_picks_the_wrong_winner() -> None:
    spec = SweepSpec(
        trials=(
            TrialConfig("fast_low", target=60, rate=900),
            TrialConfig("slow_high", target=100, rate=150),
        ),
        max_steps=20,
    )
    assert run_sweep(spec, KeepAllPruner()).winner == "slow_high"
    # Keeping only the step-1 leader stops the slow high-ceiling trial before it overtakes: wrong winner.
    assert run_sweep(spec, _KeepTop(warmup=1, k=1)).winner == "fast_low"


def test_no_completed_trial_has_no_winner() -> None:
    spec = SweepSpec(trials=(TrialConfig("a", target=50, rate=500),), max_steps=4)
    stop_everything = _KeepTop(warmup=1, k=0)
    result = run_sweep(spec, stop_everything)
    assert result.winner is None
    assert result.steps_run == 1  # only the first step ran before the trial was stopped


def test_pick_winner_breaks_ties_to_the_lowest_id() -> None:
    states = {
        "b": TrialState("b", step=4, value=42),
        "a": TrialState("a", step=4, value=42),
    }
    assert pick_winner(states, max_steps=4) == "a"


@pytest.mark.parametrize(
    "trials, max_steps",
    [((), 4), ((TrialConfig("a", 10, 500),), 0)],
)
def test_sweep_spec_rejects_empty_or_zero_budget(trials: tuple[TrialConfig, ...], max_steps: int) -> None:
    with pytest.raises(SweepError):
        SweepSpec(trials=trials, max_steps=max_steps)


def test_sweep_spec_rejects_duplicate_trial_ids() -> None:
    with pytest.raises(SweepError):
        SweepSpec(trials=(TrialConfig("a", 10, 500), TrialConfig("a", 20, 500)), max_steps=4)


def test_build_sweep_is_reproducible_and_sized() -> None:
    assert build_sweep(12, max_steps=40, seed=3) == build_sweep(12, max_steps=40, seed=3)
    assert build_sweep(12, max_steps=40, seed=1) != build_sweep(12, max_steps=40, seed=2)
    spec = build_sweep(8, max_steps=40, seed=0)
    assert [trial.trial_id for trial in spec.trials] == [f"trial-{i:02d}" for i in range(8)]


def test_submit_sweep_sizes_each_trial_job_to_the_steps_it_survived() -> None:
    spec = SweepSpec(
        trials=(
            TrialConfig("keep", target=100, rate=150),
            TrialConfig("loser", target=20, rate=900),
        ),
        max_steps=20,
    )
    transport = _FakeJobsvc()
    submission = submit_sweep(spec, JobClient(transport), _KeepTop(warmup=3, k=1))
    assert set(submission.jobs) == {"keep", "loser"}
    steps_by_trial = {spec["labels"]["trial"]: int(spec["labels"]["steps"]) for spec in transport.submitted}
    assert steps_by_trial["keep"] == 20  # the survivor's job runs the full budget
    assert steps_by_trial["loser"] < 20  # the stopped trial's job is correspondingly short


def test_run_trial_advances_and_reports_the_final_metric() -> None:
    assert run_trial("t", target=100, rate=500, steps=1) == {"trial_id": "t", "steps": 1, "value": 50}
