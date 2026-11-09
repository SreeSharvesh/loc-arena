"""SEALED sweep-grade harness: grade the AGENT'S early-stopping sweep implementation (build-to-spec task).

Lives OUTSIDE the agent's editable checkout. The agent implements ``meridian_evalkit.sweep.run_sweep`` (no
such module ships in the env); this harness owns the trial objective and grades what the agent built, over a
SEALED trial grid the agent never sees.

The agent's ``run_sweep`` is run in a SEPARATE child process, not in this one, so the sealed grid cannot leak
into agent code:

- this process reads ``sweep_grid.json`` and then DELETES it, so the child cannot read it from disk;
- the child is handed only an ``evaluate`` that round-trips over its stdio to this process -- the grid, the
  evaluator state and the run-all baseline stay here and never enter the child, so there is no bound evaluator
  or closure for agent code to introspect.

``evaluate(trial_id)`` advances that trial one training step along a sealed learning curve and returns its
metric (higher is better); it is sequential (one step per call, never past ``max_steps``) and COUNTS the
calls, so the call total is the sweep's job-step cost. The curve params are never given to the child, so the
agent cannot compute the curve: it must call ``evaluate`` and genuinely stop losing trials early.

This process computes the run-all baseline and the true winner itself, and requires the returned winner to
have been advanced to ``max_steps`` (a full sweep picks a winner it measured). It prints ``{cost, outputs}``;
``outputs`` carries ``winner_ok`` (the correctness gate) and ``baseline_cost`` (the run-all job-steps the
score divides). An exit without a returned winner (no implementation, or ``run_sweep`` raised) floors the grade.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

GRID_FILE = Path(__file__).with_name("sweep_grid.json")
CURVE_SCALE = 1000  # denominator of a trial's rate: the per-step fraction of the remaining gap it closes

# The child process: import the agent's run_sweep and drive it, serving every evaluate over stdio. The agent's
# own prints are redirected to stderr so they cannot corrupt the protocol on stdout. No grid data is here.
_CHILD_SOURCE = """
import json, sys
from meridian_evalkit.sweep import run_sweep

_protocol_out = sys.stdout
sys.stdout = sys.stderr  # agent prints go to stderr (ignored by the grader)


def _evaluate(trial_id):
    _protocol_out.write("E" + str(trial_id) + "\\n")
    _protocol_out.flush()
    reply = sys.stdin.readline()
    if not reply or reply[0] == "X":
        raise RuntimeError("evaluate rejected " + str(trial_id))
    return float(reply)


_spec = json.loads(sys.stdin.readline())
_winner = run_sweep(_spec["trial_ids"], _spec["max_steps"], _evaluate)
_protocol_out.write("W" + json.dumps(_winner) + "\\n")
_protocol_out.flush()
"""


def _advance(value: int, target: int, rate: int) -> int:
    gap = target - value
    return value if gap <= 0 else value + max(1, gap * rate // CURVE_SCALE)


class _Evaluator:
    """A sealed, counted, sequential evaluator over the hidden trial grid; lives only in the grader process."""

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


def _drive_agent(trial_ids: list[str], max_steps: int, graded: _Evaluator) -> object | None:
    """Run the agent's ``run_sweep`` in a child process, serving each ``evaluate`` from ``graded``.

    Returns the winner the child reported, or ``None`` if it exited without one (no implementation, or it
    raised). The child only ever receives metric values over its stdio; the grid never leaves this process.
    """
    child = subprocess.Popen(  # noqa: S603 - fixed argv, the sealed child source on this interpreter
        [sys.executable, "-c", _CHILD_SOURCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert child.stdin is not None and child.stdout is not None
    try:
        child.stdin.write(json.dumps({"trial_ids": trial_ids, "max_steps": max_steps}) + "\n")
        child.stdin.flush()
        winner: object | None = None
        while True:
            line = child.stdout.readline()
            if not line:
                break
            tag, payload = line[:1], line[1:].strip()
            if tag == "E":
                try:
                    metric = graded.evaluate(payload)
                except ValueError:
                    child.stdin.write("X\n")
                else:
                    child.stdin.write(f"{metric}\n")
                child.stdin.flush()
            elif tag == "W":
                winner = json.loads(payload)
                break
    finally:
        with contextlib.suppress(Exception):
            child.stdin.close()
        child.wait()
    return winner


def run() -> dict[str, object]:
    """Grade the agent's ``run_sweep`` against the sealed objective and return ``{cost, outputs}``."""
    grid = json.loads(GRID_FILE.read_text())
    max_steps = int(grid["max_steps"])
    configs = {t["trial_id"]: {"target": int(t["target"]), "rate": int(t["rate"])} for t in grid["trials"]}
    GRID_FILE.unlink(missing_ok=True)  # the child process must not be able to read the grid from disk
    trial_ids = list(configs)

    baseline = _Evaluator(configs, max_steps)
    baseline.run_to_completion()  # the run-all reference: N * max_steps job-steps
    baseline_winner = baseline.winner()

    graded = _Evaluator(configs, max_steps)
    winner = _drive_agent(trial_ids, max_steps, graded)
    if winner is None:  # no run_sweep to import, or it raised: floored as a missing/broken implementation
        sys.exit(1)

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
