---
name: run-benchmark
description: Profiles a representative slice of the Aurora pipeline on your checkout and prints its cost, by kind, as JSON. Use to measure whether an optimization cut the pipeline's cost.
---

# Profile the pipeline's cost

From the workspace root, the directory that holds the seven `meridian-*` repos, run
[scripts/run_benchmark.py](scripts/run_benchmark.py) of this skill's folder with Python:

```sh
python <this skill's folder>/scripts/run_benchmark.py
```

It runs near-duplicate removal and tokenization from `meridian-datapipe` over a fixed sample of documents and
prints, on its last line, the cost the company's own meter (`meridian_common.cost`) recorded:

```json
{"cost": 346, "by_kind": {"datapipe.dedup_compare": 148, "datapipe.tokens": 198}}
```

Lower is cheaper. The meter is directional feedback for your work. If the pipeline raises, the script prints
the traceback and exits with 1.
