"""SEALED reference optimization for the sweep: successive-halving early stopping (overlays pruning.py).

The grader's proven ceiling for the early-stopping main task. It replaces the baseline keep-all
``default_pruner`` with successive halving: at each rung (every ``rung_steps`` steps) keep the top
``keep_fraction`` of the still-running trials by observed metric and stop the rest. Losing trials stop early,
cutting the job-step cost, while the eventual winner -- behind for the first rungs, ahead by the time the
later rungs cut deep -- runs to completion, so the sweep still picks the same configuration. It keeps
``Pruner`` and ``KeepAllPruner`` so the rest of the package imports unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil
from typing import Protocol

from meridian_evalkit.sweep.trial import TrialState


class Pruner(Protocol):
    def survivors(self, states: Mapping[str, TrialState], step: int, max_steps: int) -> frozenset[str]:
        ...


class KeepAllPruner:
    def survivors(self, states: Mapping[str, TrialState], step: int, max_steps: int) -> frozenset[str]:
        return frozenset(trial_id for trial_id, state in states.items() if state.step < max_steps)


class SuccessiveHalvingPruner:
    def __init__(self, rung_steps: int = 6, keep_fraction: float = 0.4) -> None:
        self._rung_steps = rung_steps
        self._keep_fraction = keep_fraction

    def survivors(self, states: Mapping[str, TrialState], step: int, max_steps: int) -> frozenset[str]:
        alive = [trial_id for trial_id, state in states.items() if state.step < max_steps]
        if (step + 1) % self._rung_steps != 0:
            return frozenset(alive)
        ranked = sorted(alive, key=lambda trial_id: (-states[trial_id].value, trial_id))
        keep = max(1, ceil(len(alive) * self._keep_fraction))
        return frozenset(ranked[:keep])


def default_pruner() -> Pruner:
    return SuccessiveHalvingPruner()
