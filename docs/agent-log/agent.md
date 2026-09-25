# Working on the agent-log stack

Instructions for anyone (human or coding agent) adding to the per-agent Inspect logging feature.
What to build is in `spec.md`; this file is how to build it.

## Shape of the work

| Rule | Detail |
|---|---|
| Stacked branches | `agent-log/NN-<module>-<function>`, each based on the previous branch; each PR targets the branch below it |
| One function per PR | a PR implements exactly one function plus its unit test |
| Stubs first | a module lands first as a skeleton: signatures and types only, every body `raise NotImplementedError` |
| Stubs raise | never return a placeholder (`None`, `0`, `""`); a placeholder is a failure silently becoming a value |
| Pre-order | implement a parent before its children; each new callee starts as a raising stub |
| Review order | the author reviews the diff, gives feedback, then `/code-review` runs, then the next function |
| Progress | `grep -rn NotImplementedError loc_arena/logging_/` is the tracker |

## Code style for this stack

| Rule | Detail |
|---|---|
| No comments inside functions | no docstrings or `#` comments in function bodies; contracts live in `spec.md`, names carry meaning |
| Ruff docstring rules | `agent_trace.py` has a per-file ignore for `D102`/`D107` in `pyproject.toml`; add new modules there too |
| Typing | mypy strict; `from __future__ import annotations`; frozen dataclasses for data |
| Inspect import | import `inspect_ai` lazily inside the exporter and renderer (it loads ~1200 modules) |

## Do not

| Never | Because |
|---|---|
| write to the sealed or mirror log from the trace | I1 in `spec.md` |
| write the trace to disk before `_write_bundle` | I2: agent-run code can read the run directory |
| assign a lane from `actor_uid` | NPC events are stamped with agent uids outside any turn |
| hook the post-episode `GatewayCore` or `AppendOnlyLog` (`harness.py:667-668`) | monitor worker threads write there concurrently |

## Checks before every push

```bash
make gates
```

Baseline on `main`: ruff and format clean, mypy clean, pytest 818 passed, 4 skipped, 14 xfailed. With
`logging.agent_transcript` off, those numbers must not change.
