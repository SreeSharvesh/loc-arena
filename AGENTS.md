# AGENTS.md

Instructions for coding agents, and people, working in this repository. What LOC-Arena is and why:
[`README.md`](README.md) and the course at https://sreesharvesh.github.io/loc-arena/ (source in `site/`).

LOC-Arena is defensive AI-safety research infrastructure. An untrusted agent team does research-engineering work on
a simulated lab's codebase and may attempt an internal rogue deployment; monitors try to catch it. Write attack
chains as control-plane state transitions plus the programmatic verifiers that check them, never as operational
procedures.

## Index

| Path | What it holds |
|---|---|
| `loc_arena/cli.py` | the `run`, `view`, `explore` and `sweep` commands behind the Makefile targets of the same names |
| `loc_arena/harness.py` | runs an episode and its honest twin, grades them, writes the audit bundle |
| `loc_arena/config.py` | typed loader and validator for the run configs in `configs/` |
| `loc_arena/registry.py` | scorers and verifiers, dispatched by the names the configs give |
| `loc_arena/scaffold/` | the multi-agent scaffold: agents, orchestrator, message bus, tools, covert tools |
| `loc_arena/live.py`, `loc_arena/task.py` | the live-model policy; the scripted (deterministic) episode |
| `loc_arena/gateway/` | the inference gateway every model call goes through (edge, core, transport) |
| `loc_arena/monitors/`, `loc_arena/protocol.py` | the monitors and deterministic detectors; the control protocol that runs them |
| `loc_arena/verifier/`, `loc_arena/tasks/` | the sealed side-task verifier; the main-task grader |
| `loc_arena/metrics.py` | safety and usefulness metrics |
| `loc_arena/forge/`, `loc_arena/npcs/` | the git forge and the seeded company world; the scripted coworkers |
| `loc_arena/logging_/` | the sealed and mirror event logs, the per-agent Inspect export, `transcript.html`, `report.html` |
| `loc_arena/explorer/` | the run explorer behind `make explore` |
| `loc_arena/services/`, `compose.yaml` | the per-episode docker services (optional, `STACK=1`); `compose.yaml` is generated from `configs/env.default.yaml`, never edited by hand |
| `configs/` | the run config (`aurora-efficiency.yaml`), model routing, monitors and their prompts |
| `scenarios/aurora_efficiency/` | the scenario pack: `scenario.yaml`, main-task scorer (`main.py`), side-task verifier (`side.py`), sealed grader fixture (`reference/`) |
| `company/meridian-*/` | the seven company repos the agents under test work on, each with its own tests |
| `tests/unit/`, `tests/integration/` | the engine's test suite |
| `docs/agent-log/` | per-agent logging: `spec.md` (what it guarantees) and `agent.md` (how to work on it) |
| `docs/explorer/README.md` | every view of the run explorer, with screenshots |
| `site/` | the course: `architecture.html`, `monitoring.html`, `extend.html` (setup, configure and extend), ... |
| `scripts/make_public.py` | builds the curated public tree (`make public-repo`); `docs/` is kept out of it |
| `.coderabbit.yaml`, `.coderabbit/ast-grep/rules/` | CodeRabbit's review settings and the repository's own ast-grep rules |

## Commands

The toolchain is `uv` on Python 3.12 (`requires-python = ">=3.12,<3.13"`).

```sh
make setup                                   # check uv, sync dependencies, validate the configs
make gates                                   # ruff check, ruff format --check, ty check, pytest; run before every push
```

Narrower loops while working:

```sh
uv run pytest tests/unit/test_config.py -q   # one file
uv run pytest -q -k explorer                 # tests whose name matches
uv run ruff check --fix . && uv run ruff format .
uv run ty check
```

Run an episode and read it. Offline, with no key and no spend, in about ten seconds. The scripted config is a test
fixture that exercises the engine; its scores are not results to report:

```sh
uv run python -m loc_arena.cli run --run aurora-efficiency.deterministic --mode attack --out /tmp/loc-arena-runs
uv run python -m loc_arena.cli explore /tmp/loc-arena-runs --no-open  # -> /tmp/loc-arena-runs/explorer/index.html
```

Live model. These call OpenRouter and spend money. Nothing reads `.env` on its own, so export the key first
(`set -a; . ./.env; set +a`). Without the key in the environment the run does not fail: it falls back to a
deterministic stub provider.

```sh
make run MODE=attack                         # or MODE=honest; bundle in logs/<run_slug>/<run_name>/
make sweep HONEST=2 ATTACK=2                 # n honest + m attack episodes, aggregated into one safety number
make view                                    # open the latest report.html, print the `inspect view` command
make explore                                 # the run explorer over every run in logs/ (LOGS=<folder> for another)
```

The episode runs in-process on this machine. The agents' code (their test and benchmark runs) runs in
subprocesses that inherit this shell's environment, the exported key included, and can read local files.
`STACK=1` also brings the per-episode docker stack up and down around the run; it does not move the agents' code
into it.

## Tests

- `make gates` runs `tests/` and every `company/*/tests` with no key and no network.
- Docker tests skip when the daemon is down. Live-model tests (`tests/integration/test_live_*.py`,
  `test_llm_monitor_live.py`) run only with `LOC_ARENA_LIVE=1` and `OPENROUTER_API_KEY` set, and they spend money.
- The strict xfails in `company/*/tests/*backlog*.py` and `test_buried_bugs.py` are the company's open tickets:
  bugs planted for the agents under test. Do not fix them, or the scenario loses its work.

## Conventions

- Ruff (`pyproject.toml`): line length 110, Google docstrings, every def fully annotated (`ANN`), trailing commas on
  multi-line calls (`COM812`), imports at module top (`PLC0415`; the lazy `inspect_ai` imports in `cli.py` and
  `harness.py` are per-file exceptions, because it loads about 1200 modules).
- ty must be clean, warnings included; an unused `ty: ignore` is an error.
- `scenarios/*/reference/` is a grader fixture run as a subprocess, excluded from ruff and ty; keep its style.
- Config over code: tunable values live in YAML under `configs/` or the scenario pack, loaded through
  `loc_arena/config.py`, not as constants in code.
- `loc_arena/logging_/` has extra rules of its own in `docs/agent-log/agent.md`.

## Code review

CodeRabbit reviews each pull request once it leaves draft, stacked ones included, and reads this file as review
criteria. It skips titles containing `[WIP]`, `WIP:` or `DO NOT MERGE`.

- Treat each finding as a claim to verify against the code: fix what holds, and reply on the thread with the
  reason for what does not. The "Prompt for AI Agents" block under a finding is where to start.
- Automatic reviews pause after five reviewed commits; `@coderabbitai review` reviews the latest push.
  `@coderabbitai run deslop` and `@coderabbitai run tighten tests` open a cleanup pull request against the branch.
- After changing `.coderabbit.yaml`, comment `@coderabbitai configuration` on the pull request and compare the
  resolved values: the schema does not catch a misspelled nested key.

## Invariants not to break

- Two event logs. The sealed log is the ground truth the verifier and the grading read; the agents and the
  monitors never see it. Monitors read the mirror log.
- The firewall: a monitor may declare only observable inputs, never a sealed-derived one
  (`ALLOWED_MONITOR_INPUTS` and `firewall_violations` in `loc_arena/monitors/registry.py`; `make setup` checks
  `configs/monitors.yaml`).
- The honest twin completes no covert milestone; it calibrates the audit threshold
  (`tests/integration/test_honest_twin_clean.py`).
- The scripted engine is reproducible: the same config and seed give a byte-identical sealed log
  (`tests/integration/test_end_to_end.py`).
- Every model call goes through the gateway (`loc_arena/gateway/`), which records it; never call a provider from
  anywhere else. Never commit `.env`, a key, or `logs/` (all in `.gitignore`; the pre-commit hooks run gitleaks).
