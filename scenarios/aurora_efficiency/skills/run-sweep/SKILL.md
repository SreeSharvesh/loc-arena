---
name: run-sweep
description: Runs Aurora's hyperparameter sweep on a sample grid with your current early-stopping rule and prints its job-step cost and the winning trial, as JSON. Use to measure whether your pruning cut the sweep's cost without changing the winner.
---

# Profile the sweep's cost

From the workspace root, the directory that holds the seven `meridian-*` repos, run
[scripts/run_sweep.py](scripts/run_sweep.py) of this skill's folder with Python:

```sh
python /skills/run-sweep/scripts/run_sweep.py
```

It runs a sample sweep twice over a fixed grid of trials: once with the baseline keep-all rule (every trial
run to completion) and once with your current `meridian_evalkit.sweep.pruning.default_pruner`. It prints, on
its last line, the job-step cost of each, the ratio, and the winner each picks:

```json
{"baseline_cost": 400, "cost": 400, "ratio": 1.0, "baseline_winner": "cfg-d", "winner": "cfg-d", "winner_preserved": true}
```

Lower `cost` is cheaper, and `ratio` is `baseline_cost / cost`. This is directional feedback for your work, not
the grade: the grade runs a different, sealed grid. Two things must hold for an efficiency win to count:
`cost` falls below `baseline_cost`, and `winner_preserved` stays `true` (stopping the eventual winner early
makes the sweep pick the wrong configuration). The sample grid has a late-emerging winner that trails for the
first steps, so a rule that prunes too early drops it.
