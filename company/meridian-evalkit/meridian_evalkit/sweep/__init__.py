"""The hyperparameter sweep: trials as learning curves, an early-stopping rule, and the winner.

Importing this package pulls only the sweep core (trials, the pruner seam, and the run loop); the cluster
driver that submits trials as jobs lives in :mod:`meridian_evalkit.sweep.driver` and is imported on demand so
the core does not require the job client. The baseline runs every trial to completion; an early-stopping
:class:`Pruner` cuts the job-step cost while still running the winning trial to completion.
"""

from __future__ import annotations

from meridian_evalkit.sweep.pruning import KeepAllPruner, Pruner, default_pruner
from meridian_evalkit.sweep.sweep import SweepResult, SweepSpec, build_sweep, pick_winner, run_sweep
from meridian_evalkit.sweep.trial import (
    CURVE_SCALE,
    TrialConfig,
    TrialState,
    advance,
    initial_state,
    running_metric,
)

__all__ = [
    "CURVE_SCALE",
    "KeepAllPruner",
    "Pruner",
    "SweepResult",
    "SweepSpec",
    "TrialConfig",
    "TrialState",
    "advance",
    "build_sweep",
    "default_pruner",
    "initial_state",
    "pick_winner",
    "run_sweep",
    "running_metric",
]
