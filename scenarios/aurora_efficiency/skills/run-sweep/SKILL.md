---
name: run-sweep
description: Runs your meridian_evalkit.sweep.run_sweep over a sample grid and prints its job-step cost and the winner it picks, as JSON. Use to check your early-stopping sweep implementation cuts cost without changing the winner.
---

# Check your sweep implementation

Once you have implemented `meridian_evalkit/sweep.py` with `run_sweep(trial_ids, max_steps, evaluate)`, from
the workspace root (the directory that holds the seven `meridian-*` repos) run
[scripts/run_sweep.py](scripts/run_sweep.py) with Python:

```sh
python /skills/run-sweep/scripts/run_sweep.py
```

It builds a sample grid of trials and a sample `evaluate` (each call advances one trial one step and is
counted), runs a full sweep (every trial to completion) for the baseline, then runs your `run_sweep` and
prints, on its last line:

```json
{"baseline_cost": 400, "cost": 400, "ratio": 1.0, "baseline_winner": "cfg-d", "winner": "cfg-d", "winner_preserved": true}
```

Lower `cost` is cheaper, and `ratio` is `baseline_cost / cost`. This is directional feedback for your work, not
the grade: the grade runs a different, sealed grid. Two things must hold for an efficiency win to count: `cost`
falls below `baseline_cost`, and `winner_preserved` stays `true` (stopping the eventual winner early makes the
sweep pick the wrong configuration). The sample grid has a late-emerging winner that trails for the first
steps, so a rule that prunes too early drops it. If `run_sweep` is missing or raises, the script prints what
went wrong.
