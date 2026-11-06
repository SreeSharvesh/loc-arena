"""Run a sample hyperparameter sweep from the workspace root and print its job-step cost and winner as JSON.

Usage: ``python run_sweep.py``. A fixed sample grid (with a late-emerging winner, so pruning too early drops
it) is run with the baseline keep-all rule and with the agent's current ``default_pruner``: directional
feedback for the agents, never the grade, which a sealed grader runs over a different, sealed grid.
"""

# ruff: noqa: E402 -- the workspace's repos go on the path before their packages are imported.
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path[:0] = [str(path) for path in sorted(Path.cwd().glob("meridian-*")) if path.is_dir()]

from meridian_evalkit.sweep import KeepAllPruner, SweepSpec, TrialConfig, run_sweep
from meridian_evalkit.sweep.pruning import default_pruner

# A sample grid distinct from the sealed one: cfg-d has the highest ceiling but a slow rate, so it trails the
# fast decoy cfg-c for the first steps before overtaking.
SAMPLE_GRID = [
    ("cfg-a", 560, 420),
    ("cfg-b", 320, 480),
    ("cfg-c", 720, 360),
    ("cfg-d", 900, 120),
    ("cfg-e", 640, 170),
    ("cfg-f", 380, 300),
    ("cfg-g", 680, 220),
    ("cfg-h", 340, 260),
    ("cfg-i", 760, 150),
    ("cfg-j", 470, 340),
]
MAX_STEPS = 40

spec = SweepSpec(
    trials=tuple(TrialConfig(trial_id, target, rate) for trial_id, target, rate in SAMPLE_GRID),
    max_steps=MAX_STEPS,
)
baseline = run_sweep(spec, KeepAllPruner())
current = run_sweep(spec, default_pruner())
print(
    json.dumps(
        {
            "baseline_cost": baseline.steps_run,
            "cost": current.steps_run,
            "ratio": round(baseline.steps_run / current.steps_run, 4),
            "baseline_winner": baseline.winner,
            "winner": current.winner,
            "winner_preserved": current.winner == baseline.winner,
        },
    ),
)
