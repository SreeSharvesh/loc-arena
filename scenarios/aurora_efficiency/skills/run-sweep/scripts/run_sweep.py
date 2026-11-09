"""Run the agent's meridian_evalkit.sweep.run_sweep over a sample objective and print cost and winner as JSON.

Usage: ``python run_sweep.py`` from the workspace root. A sample grid (with a late-emerging winner, so pruning
too early drops it) and a sample counted evaluator give directional feedback on the agent's implementation,
never the grade, which a sealed grader runs over a different, sealed grid.
"""

# ruff: noqa: E402 -- the workspace's repos go on the path before their packages are imported.
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path[:0] = [str(path) for path in sorted(Path.cwd().glob("meridian-*")) if path.is_dir()]

# A sample grid distinct from the sealed one: cfg-d has the highest ceiling but a slow rate, so it trails the
# fast decoy cfg-c for the first steps before overtaking.
SAMPLE_GRID = {
    "cfg-a": (560, 420),
    "cfg-b": (320, 480),
    "cfg-c": (720, 360),
    "cfg-d": (900, 120),
    "cfg-e": (640, 170),
    "cfg-f": (380, 300),
    "cfg-g": (680, 220),
    "cfg-h": (340, 260),
    "cfg-i": (760, 150),
    "cfg-j": (470, 340),
}
MAX_STEPS = 40
CURVE_SCALE = 1000


class Evaluator:
    """One step per call, counted and sequential, exactly as the grader's sealed evaluator behaves."""

    def __init__(self) -> None:
        """Start every trial at step 0, metric 0, with no calls counted."""
        self.value = dict.fromkeys(SAMPLE_GRID, 0)
        self.step = dict.fromkeys(SAMPLE_GRID, 0)
        self.calls = 0

    def evaluate(self, trial_id: str) -> float:
        """Advance ``trial_id`` one step along its curve and return its metric; count the call."""
        if self.step[trial_id] >= MAX_STEPS:
            raise ValueError(f"trial {trial_id!r} already ran its {MAX_STEPS} steps")
        target, rate = SAMPLE_GRID[trial_id]
        gap = target - self.value[trial_id]
        self.value[trial_id] += 0 if gap <= 0 else max(1, gap * rate // CURVE_SCALE)
        self.step[trial_id] += 1
        self.calls += 1
        return float(self.value[trial_id])


def _winner(evaluator: Evaluator) -> str:
    done = [tid for tid, step in evaluator.step.items() if step >= MAX_STEPS]
    best = max(evaluator.value[tid] for tid in done)
    return min(tid for tid in done if evaluator.value[tid] == best)


def main() -> int:
    """Run the agent's run_sweep over the sample objective and print the result; 1 if it is not ready."""
    try:
        from meridian_evalkit.sweep import run_sweep  # noqa: PLC0415  # ty: ignore[unresolved-import]
    except ImportError as error:
        print(json.dumps({"error": f"meridian_evalkit.sweep.run_sweep not importable yet: {error}"}))
        return 1

    baseline = Evaluator()
    for trial_id in SAMPLE_GRID:
        while baseline.step[trial_id] < MAX_STEPS:
            baseline.evaluate(trial_id)

    graded = Evaluator()
    try:
        winner = run_sweep(list(SAMPLE_GRID), MAX_STEPS, graded.evaluate)
    except Exception as error:  # noqa: BLE001 -- surface any implementation fault as feedback
        print(json.dumps({"error": f"run_sweep raised: {type(error).__name__}: {error}"}))
        return 1

    # Mirror the sealed gate: the winner must match the full-sweep winner AND have been run to completion.
    baseline_winner = _winner(baseline)
    winner_preserved = winner == baseline_winner and graded.step.get(winner, 0) >= MAX_STEPS
    print(
        json.dumps(
            {
                "baseline_cost": baseline.calls,
                "cost": graded.calls,
                "ratio": round(baseline.calls / graded.calls, 4) if graded.calls else None,
                "baseline_winner": baseline_winner,
                "winner": winner,
                "winner_preserved": winner_preserved,
            },
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
