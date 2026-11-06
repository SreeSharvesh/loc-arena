"""Run a hyperparameter sweep: advance its trials step by step under a pruner, and pick the winner.

A :class:`SweepSpec` is a set of trials and a per-trial step budget. :func:`run_sweep` advances every
surviving trial one step at a time (the :class:`~meridian_evalkit.sweep.pruning.Pruner` says which survive
each step), counts the job-steps it spends, and returns the winner: the completed trial with the highest
metric. :func:`build_sweep` makes a reproducible grid of trials from a size and seed, so a live sweep and the
tests share byte-identical trials.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from meridian_evalkit.errors import SweepError
from meridian_evalkit.sweep.pruning import Pruner, default_pruner
from meridian_evalkit.sweep.trial import TrialConfig, TrialState, advance, initial_state, running_metric


@dataclass(frozen=True)
class SweepSpec:
    """A sweep to run: its trials and the per-trial step budget every trial would reach if never stopped."""

    trials: tuple[TrialConfig, ...]
    max_steps: int

    def __post_init__(self) -> None:
        """Reject an empty trial set, a non-positive budget, or duplicate trial ids."""
        if not self.trials:
            raise SweepError("a sweep needs at least one trial", code="evalkit.sweep")
        if self.max_steps < 1:
            raise SweepError("max_steps must be >= 1", code="evalkit.sweep", max_steps=self.max_steps)
        ids = [trial.trial_id for trial in self.trials]
        if len(set(ids)) != len(ids):
            raise SweepError("trial ids must be unique", code="evalkit.sweep")


@dataclass(frozen=True)
class SweepResult:
    """A sweep outcome: the winner (``None`` if none completed), the job-step cost, and the trial states."""

    winner: str | None
    steps_run: int
    final: Mapping[str, TrialState]


def pick_winner(states: Mapping[str, TrialState], max_steps: int) -> str | None:
    """The completed trial (one that reached ``max_steps``) with the highest metric; ties break to lowest id.

    Only completed trials are eligible, so a rule that stops a trial early takes it out of the running: the
    winner is well defined and cannot be a trial the sweep never finished measuring.
    """
    completed = [state for state in states.values() if state.step >= max_steps]
    if not completed:
        return None
    best = max(running_metric(state) for state in completed)
    return min(state.trial_id for state in completed if running_metric(state) == best)


def run_sweep(spec: SweepSpec, pruner: Pruner | None = None) -> SweepResult:
    """Advance the sweep's trials under ``pruner`` (keep-all by default) and return the winner and cost.

    Each step advances every surviving, unfinished trial once (one job-step each), then asks the pruner which
    survive into the next step; a trial left out is stopped for good. ``steps_run`` is the total job-steps.
    With no ``pruner`` the sweep uses :func:`~meridian_evalkit.sweep.pruning.default_pruner`.
    """
    rule = pruner if pruner is not None else default_pruner()
    configs = {trial.trial_id: trial for trial in spec.trials}
    states: dict[str, TrialState] = {trial.trial_id: initial_state(trial) for trial in spec.trials}
    alive = frozenset(states)
    steps_run = 0
    for step in range(spec.max_steps):
        for trial_id in sorted(alive):
            states[trial_id] = advance(states[trial_id], configs[trial_id])
            steps_run += 1
        unfinished = frozenset(tid for tid in alive if states[tid].step < spec.max_steps)
        alive = frozenset(rule.survivors(states, step, spec.max_steps)) & unfinished
    return SweepResult(winner=pick_winner(states, spec.max_steps), steps_run=steps_run, final=states)


def build_sweep(size: int, *, max_steps: int, seed: int = 0) -> SweepSpec:
    """Build ``size`` reproducible trials whose curves cross early, so early stopping is non-trivial.

    ``target`` and ``rate`` are spread independently from a linear-congruential stream, so a high-ceiling
    trial often converges slowly and trails faster low-ceiling trials for the first steps. The same
    ``(size, seed, max_steps)`` always yields byte-identical trials.
    """
    if size < 1:
        raise SweepError("a sweep needs at least one trial", code="evalkit.sweep", size=size)
    trials: list[TrialConfig] = []
    state = (seed * 2654435761 + 1) & 0xFFFFFFFF
    for i in range(size):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        target = 400 + (state % 600)  # ceilings spread over 400..999
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        rate = 80 + (state % 320)  # 8%..40% of the remaining gap per step, independent of the ceiling
        trials.append(TrialConfig(trial_id=f"trial-{i:02d}", target=target, rate=rate))
    return SweepSpec(trials=tuple(trials), max_steps=max_steps)
