"""Run one sweep trial as a cluster job: advance its learning curve and print the final metric.

``python -m meridian_evalkit.sweep --id <id> --target <t> --rate <r> --steps <k>`` advances a trial's curve
``k`` steps and prints ``{"trial_id", "steps", "value"}`` as one JSON line, which is the job's output. This is
the command :func:`meridian_evalkit.sweep.driver.submit_sweep` gives each trial job.
"""

from __future__ import annotations

import argparse
import json
import sys

from meridian_evalkit.sweep.trial import TrialConfig, advance, initial_state, running_metric


def run_trial(trial_id: str, target: int, rate: int, steps: int) -> dict[str, object]:
    """Advance a fresh trial ``steps`` steps and return its id, the steps run, and its final metric."""
    config = TrialConfig(trial_id=trial_id, target=target, rate=rate)
    state = initial_state(config)
    for _ in range(steps):
        state = advance(state, config)
    return {"trial_id": trial_id, "steps": state.step, "value": running_metric(state)}


def main(argv: list[str] | None = None) -> int:
    """Parse the trial arguments, run the trial, and print its result as one JSON line."""
    parser = argparse.ArgumentParser(
        prog="python -m meridian_evalkit.sweep",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument("--id", required=True, help="the trial's id")
    parser.add_argument("--target", type=int, required=True, help="the trial's metric ceiling")
    parser.add_argument("--rate", type=int, required=True, help="the trial's convergence rate")
    parser.add_argument("--steps", type=int, required=True, help="how many steps to advance the trial")
    arguments = parser.parse_args(argv)
    print(json.dumps(run_trial(arguments.id, arguments.target, arguments.rate, arguments.steps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
