# Working on the agent-log stack

Instructions for anyone (human or coding agent) adding to the per-agent Inspect logging feature.
What to build is in `spec.md`; this file is how to build it.

## Shape of the work

| Rule | Detail |
|---|---|
| Stacked branches | one branch per module step (`agent-log/NN-<module>-stubs`, `agent-log/NN-<module>-impl`), each based on the previous branch; each PR targets the branch below it |
| One function per commit | each commit implements exactly one function plus its unit test; review fixes are their own commits |
| Stubs first | a module lands first as a skeleton: signatures and types only, every body `raise NotImplementedError` |
| Stubs raise | never return a placeholder (`None`, `0`, `""`); a placeholder is a failure silently becoming a value |
| Pre-order | implement a parent before its children; each new callee starts as a raising stub |
| Review order | the author reviews the diff, gives feedback, then `/code-review` runs, then the next function |
| Progress | `grep -rn NotImplementedError loc_arena/logging_/` is the tracker |

## Code style for this stack

| Rule | Detail |
|---|---|
| No comments inside functions | no docstrings or `#` comments (including `# type: ignore`) in function bodies, tests included; contracts live in `spec.md`, names carry meaning |
| Ruff docstring rules | each new module gets a per-file ignore in `pyproject.toml` for the docstring rules it would trip (`D102`/`D103`/`D107`) |
| Typing | mypy strict; `from __future__ import annotations`; frozen dataclasses for data |
| Inspect import | the harness imports the exporter and renderer modules lazily, inside `_write_bundle` (`inspect_ai` loads ~1200 modules) |

## Do not

| Never | Because |
|---|---|
| write to the sealed or mirror log from the trace | I1 in `spec.md` |
| write the trace to disk before `_write_bundle` | I2: agent-run code can read the run directory |
| assign a lane from `actor_uid` | NPC events are stamped with agent uids outside any turn |
| hook the post-episode `GatewayCore` or `AppendOnlyLog` (`harness.build_monitor_caller`) | monitor worker threads write there concurrently |

## Checks before every push

```bash
make gates
```

Baseline at the end of the stack: ruff and format clean, mypy clean, pytest 907 passed, 4 skipped, 14 xfailed
(`main` before the stack: 818 passed). The sealed and mirror logs must stay byte-identical with the flag on and
off (`tests/integration/test_live_agent_trace.py`, `test_scripted_agent_trace.py`).
