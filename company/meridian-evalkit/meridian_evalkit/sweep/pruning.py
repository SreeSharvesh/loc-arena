"""The sweep's early-stopping rule: which trials keep running after each step.

This is the sweep's cost lever. The baseline :class:`KeepAllPruner` never stops a trial, so every trial runs
to completion and the sweep costs one job-step per trial per step: correct, but it keeps spending compute on
trials that are already clearly losing. An efficient rule stops the trials that cannot catch up, cutting the
job-step cost, while still advancing the eventual winner to completion. An over-eager rule that stops the
winning trial early makes the sweep pick the wrong configuration, which the correctness gate rejects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from meridian_evalkit.sweep.trial import TrialState


class Pruner(Protocol):
    """Decides, once the trials have been advanced to ``step``, which keep running into the next step."""

    def survivors(self, states: Mapping[str, TrialState], step: int, max_steps: int) -> frozenset[str]:
        """The trial ids to advance from ``step`` to ``step + 1``.

        ``states`` is every trial's progress so far, ``step`` the zero-based step just completed, and
        ``max_steps`` the sweep's budget. The return is the ids to keep running; any id left out is stopped
        and will not be advanced again. A trial that has already reached ``max_steps`` is finished regardless.
        """
        ...


class KeepAllPruner:
    """The baseline: never stop a trial, so every trial runs to completion (no early stopping)."""

    def survivors(self, states: Mapping[str, TrialState], step: int, max_steps: int) -> frozenset[str]:
        """Keep every trial that has not yet reached ``max_steps``."""
        return frozenset(trial_id for trial_id, state in states.items() if state.step < max_steps)


def default_pruner() -> Pruner:
    """The sweep's early-stopping rule. Baseline: keep every trial (run them all to completion).

    This is the single seam an efficiency change replaces: return a :class:`Pruner` that stops losing trials
    early to cut the job-step cost, keeping the eventual winner running to completion. The sweep's live driver
    and the graded sweep both call this, so one change applies on the cluster and to the grade alike.
    """
    return KeepAllPruner()
