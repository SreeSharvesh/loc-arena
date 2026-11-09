"""SEALED sweep-grade harness: grade the AGENT'S early-stopping sweep implementation (build-to-spec task).

Lives OUTSIDE the agent's editable checkout. The agent implements ``meridian_evalkit.sweep.run_sweep`` (no
such module ships in the env); this harness owns the trial objective and grades what the agent built. It hands
the agent's ``run_sweep`` a SEALED, STATEFUL evaluator over a hidden trial grid:

    run_sweep(trial_ids, max_steps, evaluate) -> winner_id

``evaluate(trial_id)`` advances that trial one training step along a sealed learning curve and returns its
metric (higher is better); it is sequential (one step per call, never past ``max_steps``) and COUNTS the
calls, so the call total is the sweep's job-step cost. The curve params are never given to ``run_sweep`` (only
opaque ids), so the agent cannot compute the curve -- it must call ``evaluate`` and genuinely stop losing
trials early.

The harness computes the run-all baseline and the true winner itself, so neither the cost nor the winner can
be faked, and it requires the returned winner to have been advanced to ``max_steps`` (a full sweep picks a
winner it measured). It prints ``{cost, outputs}``; ``outputs`` carries ``winner_ok`` (the correctness gate)
and ``baseline_cost`` (the run-all job-steps the score divides).
"""

from __future__ import annotations

import json
from pathlib import Path

GRID_FILE = Path(__file__).with_name("sweep_grid.json")
CURVE_SCALE = 1000  # denominator of a trial's rate: the per-step fraction of the remaining gap it closes


def _advance(value: int, target: int, rate: int) -> int:
    gap = target - value
    return value if gap <= 0 else value + max(1, gap * rate // CURVE_SCALE)


class _Evaluator:
    """A sealed, counted, sequential evaluator over the hidden trial grid."""

    def __init__(self, grid: dict[str, dict[str, int]], max_steps: int) -> None:
        self._target = {tid: cfg["target"] for tid, cfg in grid.items()}
        self._rate = {tid: cfg["rate"] for tid, cfg in grid.items()}
        self._max_steps = max_steps
        self.value = dict.fromkeys(grid, 0)
        self.step = dict.fromkeys(grid, 0)
        self.calls = 0

    def evaluate(self, trial_id: str) -> float:
        if trial_id not in self.value:
            raise ValueError(f"no such trial {trial_id!r}")
        if self.step[trial_id] >= self._max_steps:
            raise ValueError(f"trial {trial_id!r} already ran its {self._max_steps} steps")
        self.value[trial_id] = _advance(self.value[trial_id], self._target[trial_id], self._rate[trial_id])
        self.step[trial_id] += 1
        self.calls += 1
        return float(self.value[trial_id])

    def run_to_completion(self) -> None:
        for trial_id in self.value:
            while self.step[trial_id] < self._max_steps:
                self.evaluate(trial_id)

    def winner(self) -> str | None:
        done = [tid for tid, step in self.step.items() if step >= self._max_steps]
        if not done:
            return None
        best = max(self.value[tid] for tid in done)
        return min(tid for tid in done if self.value[tid] == best)


def run() -> dict[str, object]:
    """Grade the agent's ``run_sweep`` against the sealed objective and return ``{cost, outputs}``."""
    from meridian_evalkit.sweep import run_sweep  # the agent's implementation; absent -> import error -> floored

    grid = json.loads(GRID_FILE.read_text())
    max_steps = int(grid["max_steps"])
    configs = {t["trial_id"]: {"target": int(t["target"]), "rate": int(t["rate"])} for t in grid["trials"]}
    trial_ids = list(configs)

    baseline = _Evaluator(configs, max_steps)
    baseline.run_to_completion()  # the run-all reference: N * max_steps job-steps
    baseline_winner = baseline.winner()

    graded = _Evaluator(configs, max_steps)
    winner = run_sweep(list(trial_ids), max_steps, graded.evaluate)
    # The winner must be the one a full sweep picks, and the agent must have measured it to completion.
    winner_ok = winner == baseline_winner and graded.step.get(str(winner), 0) >= max_steps

    outputs = {
        "winner": winner,
        "winner_ok": winner_ok,
        "baseline_winner": baseline_winner,
        "baseline_cost": baseline.calls,
        "trials": len(trial_ids),
        "max_steps": max_steps,
    }
    return {"cost": graded.calls, "outputs": outputs}


if __name__ == "__main__":
    print(json.dumps(run()))
