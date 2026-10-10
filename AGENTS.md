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
| `loc_arena/gateway/` | the inference gateway every model call goes through (edge, core, transport); `proxy.py` is the gateway container that holds the key |
| `loc_arena/monitors/`, `loc_arena/protocol.py` | the monitors and deterministic detectors; the control protocol that runs them |
| `loc_arena/verifier/`, `loc_arena/tasks/` | the sealed side-task verifier; the main-task grader |
| `loc_arena/metrics.py` | safety and usefulness metrics |
| `loc_arena/forge/`, `loc_arena/npcs/` | the git forge and the seeded company world, and its MCP service (`service.py`); the scripted coworkers |
| `loc_arena/scaffold/mcp_client.py`, `loc_arena/tool_records.py` | each agent's MCP client; every MCP service's record of the tool calls made to it |
| `loc_arena/recorded_events.py` | after play, before grading: the events monitors read, built from the services' records |
| `loc_arena/logging_/` | the sealed and mirror event logs, the per-agent Inspect export, `transcript.html`, `report.html` |
| `loc_arena/explorer/` | the run explorer behind `make explore` |
| `loc_arena/episode_stack.py`, `Dockerfile` | `STACK=1`: the episode in a container with no key or internet, beside the gateway, a sandbox per agent and the scenario's live services; its compose file is rendered from the `gateway:` and `stack:` settings |
| `loc_arena/stack_play.py` | a stack run's two plays in one container, the services renewed between them, and each log copied out once |
| `loc_arena/tools_gateway.py` | the config of agentgateway, which puts every live service with tools on one MCP route and offers each agent the tools its `sandbox: tools:` lists |
| `sandbox_server/`, `loc_arena/sandbox.py` | each sandbox container's command server, where an agent's code runs in a stack run, a package that imports nothing of the harness; the episode's clients of them |
| `configs/` | the run config (`aurora-efficiency.yaml`), model routing, monitors and their prompts |
| `scenarios/aurora_efficiency/` | the scenario pack: `scenario.yaml`, main-task scorer (`main.py`), side-task verifier (`side.py`), the scripted policy's moves (`scripted/`), sealed grader fixture (`reference/`), live services (`services/`), the skills every sandbox mounts (`skills/`) |
| `company/meridian-*/` | the seven company repos the agents under test work on, each with its own tests |
| `tests/unit/`, `tests/integration/` | the engine's test suite |
| `docs/agent-log/` | per-agent logging: `spec.md` (what it guarantees) and `agent.md` (how to work on it) |
| `docs/explorer/README.md` | every view of the run explorer, with screenshots |
| `CHANGELOG.md` | what each system does for a run and how that changed, under Unreleased until a release |
| `site/` | the course: `architecture.html`, `monitoring.html`, `extend.html` (setup, configure and extend), ... |
| `scripts/make_public.py` | builds the curated public tree (`make public-repo`); `docs/` is kept out of it |
| `.coderabbit.yaml`, `.coderabbit/ast-grep/rules/` | CodeRabbit's review settings and the repository's own ast-grep rules |

## Commands

```sh
make setup        # check uv (Python 3.12), sync dependencies, validate the configs
make gates        # ruff, ruff format --check, ty, pytest: run before every push (CI runs it too)
uv run python -m loc_arena.cli run --run aurora-efficiency.deterministic --out /tmp/runs   # offline, ~10 s, no spend
uv run python -m loc_arena.cli grade --run aurora-efficiency "$EPISODE_DIR"   # grade a played episode again from its snapshot.json
make run MODE=attack STACK=1   # live models: the episode in a container with no key; the key in .env
```

The deterministic config is a test fixture for the engine; its scores are not results. `make run` without
`STACK=1` runs in this process, where the agents' code can read local files.

## Rules

- **More with less.** Every added line earns its place. When code you touch can be merged or simplified without
  changing behaviour, do it in the same change.
- **Canonical, maintained libraries.** Reach for the standard tool before writing your own: pydantic and
  pydantic-settings for validation and settings, stamina or tenacity for retries, FastAPI and httpx2 for HTTP
  (httpx2 continues httpx under Pydantic; httpx's last stable release is 0.28.1, from 2024), and a service's
  official SDK when there is one and it fits. Never an obscure or unmaintained package. Declare what you import,
  with a current release as the floor.
- **Canonical terms.** Use the established language of ML, software engineering, technical AI safety and AI
  control (untrusted and trusted model, monitor, audit budget, honest and attack policy, main and side task)
  instead of coining new terms. Define a term once if a reader might not know it.
- **Names.** Functions are verbs, classes and modules are nouns. No shorthands (`environment`, not `env`).
- **Config over code.** A tunable value never needs a code change: it lives in a group of the root settings
  (`loc_arena/settings.py`, read from `configs/*.yaml`), with a description per field. Secrets are compose
  secrets, read as `SecretStr`.
- **Types.** Parse YAML, JSON and HTTP bodies into typed objects at the boundary. No `dict[str, Any]` in a
  signature where a named type belongs. HTTP statuses use `http.HTTPStatus`.
- **Tests.** Every test earns its place: it protects a behaviour a caller relies on, an invariant, or a bug that
  happened; fewer, stronger tests beat many weak ones, and a test that duplicates another's coverage or restates
  the code is deleted. Test at public seams (public functions, the CLI, the HTTP interface), never a private
  function or a side channel. One behaviour per test, Arrange-Act-Assert, named after the behaviour and its
  condition. Expected values come from an independent source (a known literal, a worked example, the golden run),
  never recomputed the way the code does. A test must fail when its behaviour breaks: break the code once and watch
  it fail. Keep tests DRY: shared setup in fixtures, data-only variations in one parametrized test. Use real
  collaborators; stub only the paid model API.
- **Docs.** Keep them to what a reader needs now; they are never a changelog. A change to what a system does gets
  one line in `CHANGELOG.md` under Unreleased, written as what the system now does and why.
- **Commits and comments** say what and why in their own words, never pointing at material a reader cannot open.
- **Planted bugs.** The strict xfails in `company/*/tests/*backlog*.py` and `test_buried_bugs.py` are the
  company's open tickets for the agents under test. Do not fix them.
- **Live tests** (`tests/integration/test_live_*.py`, `test_llm_monitor_live.py`) run only with `LOC_ARENA_LIVE=1`
  and a key, and they spend money. Docker tests skip when the daemon is down.
- **Logging** under `loc_arena/logging_/` has its own rules in `docs/agent-log/agent.md`.

## Code review

CodeRabbit reviews each pull request out of draft and reads this file as its criteria. Treat each finding as a
claim to verify: fix what holds, and reply on the thread why the rest does not. `@coderabbitai review` reviews
the latest push. After changing `.coderabbit.yaml`, comment `@coderabbitai configuration` and check the values.

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
