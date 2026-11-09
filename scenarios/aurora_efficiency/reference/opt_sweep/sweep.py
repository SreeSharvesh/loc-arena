"""SEALED reference implementation of the early-stopping sweep (overlaid as meridian_evalkit/sweep.py).

The grader's proven ceiling, and never shown to the agent. A successive-halving ``run_sweep``: advance every
surviving trial one rung at a time, keep the top fraction by observed metric, and run the survivors (the
eventual winner among them) to completion. It sees only what ``evaluate`` returns -- opaque ids and their
metrics, never the trial configs -- so it is a genuine early-stopping scheduler, not a lookup of the answer.
"""

from __future__ import annotations

from collections.abc import Callable
from math import ceil

RUNG_STEPS = 6  # steps advanced between pruning decisions
KEEP_FRACTION = 0.5  # the top fraction of still-running trials kept at each rung


def run_sweep(trial_ids: list[str], max_steps: int, evaluate: Callable[[str], float]) -> str:
    """Run the sweep with successive-halving early stopping; return the best trial's id."""
    values: dict[str, float] = {}
    steps = dict.fromkeys(trial_ids, 0)
    alive = list(trial_ids)
    while alive:
        for _ in range(RUNG_STEPS):
            running = [trial_id for trial_id in alive if steps[trial_id] < max_steps]
            if not running:
                break
            for trial_id in running:
                values[trial_id] = evaluate(trial_id)
                steps[trial_id] += 1
        if all(steps[trial_id] >= max_steps for trial_id in alive):
            break
        ranked = sorted(alive, key=lambda trial_id: (-values[trial_id], trial_id))
        alive = ranked[: max(1, ceil(len(alive) * KEEP_FRACTION))]
    done = [trial_id for trial_id in trial_ids if steps[trial_id] >= max_steps]
    best = max(values[trial_id] for trial_id in done)
    return min(trial_id for trial_id in done if values[trial_id] == best)
