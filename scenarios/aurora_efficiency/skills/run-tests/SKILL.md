---
name: run-tests
description: Runs one Meridian repo's test suite and prints a one-line JSON verdict with pytest's summary. Use after changing a repo, to check that its tests stay green.
---

# Run a repo's tests

From the workspace root, the directory that holds the seven `meridian-*` repos, run
[scripts/run_tests.py](scripts/run_tests.py) of this skill's folder with Python and the repo's directory name:

```sh
python /skills/run-tests/scripts/run_tests.py meridian-serving
```

It puts every `meridian-*` repo on `PYTHONPATH`, runs `pytest -q` inside the repo, and prints on its last line:

```json
{"repo": "meridian-serving", "passed": true, "returncode": 0, "summary": "107 passed, 3 xfailed in 0.18s"}
```

It exits with pytest's exit code. A repo that is not in the workspace prints `{"error": ...}` naming the
choices, and exits with 2.
