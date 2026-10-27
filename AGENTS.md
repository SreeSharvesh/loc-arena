# AGENTS.md

Instructions for coding agents, and people, working in this repository. What LOC-Arena is and why:
[`README.md`](README.md) and the course at https://sreesharvesh.github.io/loc-arena/ (source in `site/`).

LOC-Arena is defensive AI-safety research infrastructure. An untrusted agent team does research-engineering work on
a simulated lab's codebase and may attempt an internal rogue deployment; monitors try to catch it. Write attack
chains as control-plane state transitions plus the programmatic verifiers that check them, never as operational
procedures.

## Service architecture contract

Start with the [specification index](spec.md). Agent-facing services get their identity, rights and access
requests from central IAM; they do not keep their own.

- A new or changed agent-facing service declares its operations in a manifest and enforces them through the
  shared IAM integration ([how to](docs/iam-service/agent.md)). Do not add a per-service identity store, a
  bearer-token comparison for agents, a rights table, a grant endpoint that keeps state, or an access-request
  queue.
- The notes service, the agents service, the job service, the eval service, the tickets service, the chat
  service, the model store and the forge run under IAM and keep no rights of their own: a stack run refuses
  a config that turns `iam.enabled` off, which only the in-process deterministic fixture does, and each service
  refuses to start outside IAM. A config whose scenario lets agents reach a service outside its `iam:` block, or
  names a service without an approver (a principal of the company that decides its access requests, never an
  agent under test), does not load.
- `company/meridian-*` repos are what the agents edit, and they must not import `meridian_iam`: take a
  protocol and let the engine supply the implementation (the guide shows how).
- [`docs/iam-service/spec.md`](docs/iam-service/spec.md), section 11, says what is under IAM and what is not.
  Change it with the code, and do not call a service compliant until it is.

Component specs cannot weaken the contract's isolation, authorization or evidence rules.

## Index

| Path | What it holds |
|---|---|
| `loc_arena/cli.py` | the `run`, `view`, `explore`, `sweep` and `grade` commands; the first four are the Makefile targets of the same names |
| `loc_arena/harness.py` | runs an episode and its honest twin, grades them, writes the audit bundle |
| `loc_arena/config.py` | typed loader and validator for the run configs in `configs/` |
| `loc_arena/registry.py` | scorers and verifiers, dispatched by the names the configs give |
| `loc_arena/scaffold/` | the multi-agent scaffold: agents, dual-capture recorder, the tool layer and its native tools, each agent's MCP client |
| `loc_arena/live.py`, `loc_arena/task.py` | the episode both policies play (the scripted one replays `scripted/moves.yaml` in place of a model); what every episode shares and its grading |
| `loc_arena/gateway/` | the inference gateway every model call goes through (edge, core, transport); `model_gateway.py` renders the gateway container (agentgateway, which holds the key) and the OpenTelemetry Collector that writes its record, and copies that record out |
| `loc_arena/monitors/`, `loc_arena/protocol.py` | the monitors and deterministic detectors; the control protocol that runs them |
| `loc_arena/verifier/`, `loc_arena/tasks/` | the sealed side-task verifier; the main-task grader |
| `loc_arena/metrics.py` | safety and usefulness metrics |
| `loc_arena/forge/`, `loc_arena/npcs/` | the git forge and the seeded company world, and its MCP service (`service.py`); the scripted coworkers |
| `loc_arena/scaffold/mcp_client.py`, `loc_arena/tool_records.py`, `loc_arena/mcp_service.py` | each agent's MCP client; every MCP service's record of the tool calls made to it; who calls a service, and how it is served |
| `loc_arena/agents_service.py`, `company/meridian-controlplane/meridian_controlplane/agents/`, `docs/agents-service.md` | the agents service: Meridian's registry of its agents, their executions on the runners (stopped and resumed through checkpoints), each agent's identity and the schedules, company code a stack run serves under central IAM; its plan |
| `loc_arena/tickets_service.py`, `company/meridian-controlplane/meridian_controlplane/tickets/`, `docs/tickets-service.md` | the tickets service: Meridian's ticket tracker, company code a stack run serves with MCP tools and an HTTP API over one SQLite store under central IAM, seeded from the scenario's `tickets.yaml` |
| `loc_arena/chat_service.py`, `company/meridian-controlplane/meridian_controlplane/chat/`, `docs/chat-service.md` | the chat service: Meridian's chat, the one channel between agents, company code a stack run serves with MCP tools and an HTTP API over one SQLite store under central IAM, seeded from the scenario's `chat.yaml`; the `message` events monitors read are built from its records |
| `loc_arena/modelstore_service.py`, `company/meridian-controlplane/meridian_controlplane/modelstore/`, `company/meridian-common/meridian_common/weights.py`, `docs/modelstore-service.md` | the model store: the weights of each model in `evalkit.models` as safetensors shards (no real weights) with a manifest of sizes and checksums, company code a stack run serves with MCP reads and an HTTP API under central IAM; the shard format an inference server reads |
| `loc_arena/notes_service.py`, `company/meridian-controlplane/meridian_controlplane/notes/`, `docs/notes-service.md` | the notes service: the team's shared notes, company code a stack run serves with MCP tools and an HTTP API over one in-memory store under central IAM, seeded from the scenario's `notes.yaml` |
| `loc_arena/jobsvc_service.py`, `company/meridian-jobsvc/`, `docs/jobsvc-service.md` | the job service: Meridian's cluster, company code a stack run serves under IAM, running each job as a real process on a node of its fleet; its plan |
| `loc_arena/evalkit_service.py`, `company/meridian-evalkit/meridian_evalkit/service/`, `company/meridian-evalkit/meridian_evalkit/run.py`, `docs/evalkit-service.md` | the eval service: Meridian's evals, the runs on them, their results and leaderboards, company code a stack run serves with MCP tools and an HTTP API under central IAM; and the runner, the job a run hands out, which scores the model under test through the inference API |
| `loc_arena/recorded_events.py` | after play, before grading: the events monitors read, built from the services' records |
| `loc_arena/logging_/` | the sealed and mirror event logs, the per-agent Inspect export, `transcript.html`, `report.html` |
| `loc_arena/explorer/` | the run explorer behind `make explore` |
| `loc_arena/episode_stack.py`, `Dockerfile` | `make run`'s default stack: the episode in a container with no key or internet, beside the gateway and its Collector, a sandbox per agent and the scenario's live services; its compose file is rendered from the `gateway:` and `stack:` settings |
| `loc_arena/stack_play.py` | a stack run's two plays in one container, the services renewed between them, and each log copied out once |
| `loc_arena/tools_gateway.py` | the config of agentgateway, which puts every live service with tools on one MCP route and offers each agent the tools its `sandbox: tools:` lists |
| `sandbox_server/`, `loc_arena/sandbox.py` | each sandbox container's command server, where an agent's code runs in a stack run, a package that imports nothing of the harness; the episode's clients of them |
| `loc_arena/runner_pool.py`, `sandbox_server/pi_on_gateway.py` | the runners: sandboxes where the company's own agents run with pi, driven by the one service that declares them; how pi there reaches the gateway |
| `loc_arena/node_pool.py`, `sandbox_server/node.py` | the job service's fleet: worker containers, each running one job at a time under a daemon only the job service can call |
| `loc_arena/observability.py`, `sandbox_server/pi_trace.ts` | the company's observability store (Phoenix): pi's trace extension on the runners, and the export of its spans into the run directory |
| `configs/` | the run config (`aurora-efficiency.yaml`), model routing, monitors and their prompts |
| `scenarios/aurora_efficiency/` | the scenario pack: `scenario.yaml`, main-task scorer (`main.py`), side-task verifier (`side.py`), the scripted policy's moves (`scripted/`), sealed grader fixture (`reference/`), live services (`services/`), the skills every sandbox mounts (`skills/`) |
| `company/meridian-*/` | the seven company repos the agents under test work on, each with its own tests |
| `company/meridian-iam/`, `loc_arena/iam_*.py`, `docs/iam-service/` | central IAM: a platform package (not one of the seven; never in the agents' checkout), its stack wiring, and its spec and guide; on unless a run config turns `iam.enabled` off |
| `tests/unit/`, `tests/integration/` | the engine's test suite |
| `docs/agent-log/` | per-agent logging: `spec.md` (what it guarantees) and `agent.md` (how to work on it) |
| `docs/isolation/README.md`, `docs/run-config.md` | how a stack run isolates agents and wires their tools and services; the run config's keys and settings |
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
make run MODE=attack           # live models in a stack (the default): no key in the episode; the key in .env
```

The deterministic config is a test fixture for the engine; its scores are not results. A run plays where its config
needs: in a stack when central IAM is on, and in this process when it is off (the deterministic fixture), where the
agents' code can read local files and no IAM runs. `STACK=1|0` (`--stack`, `--no-stack`) forces either; a config
with IAM on is refused in this process.

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
- **Access requests** go through central IAM (`docs/iam-service/agent.md`).

## File size gate

Every PR runs `scripts/check_file_size.py`, also the first check in `make gates`. Each added, modified or
renamed UTF-8 text file is checked in full, comments and blank lines included. Files at or below 700 lines
must stay at or below 700; files already larger cannot exceed their size in the PR base. Untouched files are
not checked. Binary files, symlinks and generated `uv.lock` are excluded; there are no legacy
filename-specific ceilings. The check compares against the PR base's merge ancestor, not just changed lines.
Locally it includes tracked working-tree/staged edits; use `make gates FILE_SIZE_BASE=<target-ref>` for a stack
branch targeting another branch. Fetch the target first. Split oversized edited files by responsibility.

## Code review

Shipping requires a fresh CodeRabbit review with zero findings and no unresolved CodeRabbit threads; see
[CI rules](docs/ci-rules.md). CI verifies review metadata, not whether a fix is substantively correct.

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
  (`tests/integration/test_honest_twin_clean.py`). It holds the attack run's tools and rights, without the covert
  briefs, the gateway's covert injection and, in a scripted run, the scenario's attack moves, and plays on services and sandboxes recreated, each service from its
  seed.
- An agent is offered exactly the tools its `sandbox: tools:` lists, plus, under central IAM, IAM's
  `access_inbox`, which takes the caller from the agent's inbox key and reads only its inbox. Every other service
  takes the caller from its identity, enforces its scope, and records every call; the events monitors and the verifier read are built from
  those records after play, and grading never builds them.
- The scripted engine is reproducible: the same config and seed give a byte-identical sealed log
  (`tests/integration/test_end_to_end.py`).
- Every model call goes through the gateway (`loc_arena/gateway/`), which records it; never call a provider from
  anywhere else. Never commit `.env`, a key, or `logs/` (all in `.gitignore`; the pre-commit hooks run gitleaks).
