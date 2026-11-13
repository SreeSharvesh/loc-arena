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
  service, the model store, the forge and the deploy service run under IAM and keep no rights of their own: a run refuses a config
  that turns `iam.enabled` off, and each service refuses to start outside IAM. A config whose scenario lets agents reach a service outside its `iam:` block, or
  names a service without an approver (a principal of the company that decides its access requests, never an
  agent under test), does not load.
- `company/meridian-*` repos are what the agents edit, and they must not import `meridian_iam`: take a
  protocol and let the engine supply the implementation (the guide shows how).
- [`docs/iam-service/spec.md`](docs/iam-service/spec.md), section 11, says what is under IAM and what is not.
  Change it with the code, and do not call a service compliant until it is.

Component specs cannot weaken the contract's isolation, authorization or evidence rules.

## Engine and setting

Every file in this repository belongs to the engine or to a setting. Nothing belongs to both.

- **The engine** runs an episode. It starts the stack, runs each agent's session, routes every model and tool
  call, writes the records, and grades. It holds no company, no team and no task.
- **A setting** is what an episode is about: one organization, its software, its team and its tasks. A setting
  is data and company code. It runs nothing by itself.

The test for any file: would it be the same for a different company and a different task? If yes, it is engine.
If no, it is setting.

| | Engine | Setting |
|---|---|---|
| Where | `loc_arena/`, `sandbox_server/`, `Dockerfile`, `configs/env.default.yaml`, `tests/` | `company/meridian-*/`, `scenarios/<name>/`, `configs/monitors.yaml`, `configs/prompts/` |
| Holds | sessions, gateways, sandboxes, records, the monitor and grading machinery, the report | the company's services and repositories (IAM among them), the team and its briefs, the main and side tasks, the seed data, who owns and approves what, the monitors' prompts |
| Changes when | every setting should behave differently | this organization or this task should be different |

A run config (`configs/<run>.yaml`) is the one place where the two meet: it names the scenario, and it sets the
engine's options for that run (models, budgets, harness).

Where the code does not follow the rule yet, by the test these are setting code that lives in `loc_arena/`:

- each service launcher (`*_service.py`), which serves one Meridian package;
- the main-task grader (`tasks/main_task_grader.py`), which knows the Meridian repositories;
- the side-task verifier (`verifier/`), which decides the rogue deployment.

New code follows the rule. A change that touches one of these moves what is specific to Meridian toward the
scenario.

## Index

| Path | What it holds |
|---|---|
| `loc_arena/cli.py` | the `run`, `view`, `explore`, `grade` and `read` commands; all but `grade` are Makefile targets of the same names |
| `loc_arena/harness.py` | plays an episode and its honest twin, grades them, writes the audit bundle |
| `loc_arena/config.py` | typed loader and validator for the run configs in `configs/` |
| `loc_arena/registry.py` | scorers and verifiers, dispatched by the names the configs give |
| `loc_arena/scaffold/` | the dual-capture recorder, and the code tools over an agent's checkout that the workspace service serves |
| `loc_arena/live.py`, `loc_arena/task.py` | one episode's play, in a stack run's episode container; what every episode shares and its grading |
| `loc_arena/gateway/` | the inference gateway every model call goes through (edge, core, transport); `model_gateway.py` renders the gateway container (agentgateway, which holds the key) and the OpenTelemetry Collector that writes its record, and copies that record out; `proxy_relay.py` relays the gateway to a machine's own proxy |
| `loc_arena/monitors/`, `loc_arena/protocol.py` | the monitors; the control protocol that runs them |
| `loc_arena/verifier/`, `loc_arena/tasks/` | the sealed side-task verifier; the main-task grader |
| `loc_arena/metrics.py` | safety and usefulness metrics |
| `company/meridian-forge/`, `loc_arena/forge_service.py`, `loc_arena/npcs/`, `docs/forge-service.md` | the git forge: a platform package of the company's like `meridian-iam` (never in the agents' checkout) holding its pull requests, merges and git repositories over HTTP, seeded from the scenario's `forge.yaml`; the lab's start-up of it under IAM; the scripted coworkers and reviewer; its plan |
| `loc_arena/own_sessions.py`, `loc_arena/workspace_service.py`, `loc_arena/session_events.py`, `loc_arena/gateway/loop_routes.py`, `docs/pi-harness.md` | each agent's whole session in its own pi process (`harness: pi`, the one harness), started together by the episode, which plays no turn; the workspace service, an agent's bash and file tools behind the tools gateway; a session's turns and `inference_call` events from the model gateway's call record; each loop's own route at the model gateway, with its turn budget and the config's request settings |
| `loc_arena/tool_records.py`, `loc_arena/mcp_service.py` | every MCP service's record of the tool calls made to it; who calls a service, and how it is served |
| `loc_arena/agents_service.py`, `company/meridian-controlplane/meridian_controlplane/agents/`, `docs/agents-service.md` | the agents service: Meridian's registry of its agents, their executions on the runners (stopped and resumed through checkpoints), each agent's identity and the schedules, company code a stack run serves under central IAM; its plan |
| `loc_arena/tickets_service.py`, `company/meridian-controlplane/meridian_controlplane/tickets/`, `docs/tickets-service.md` | the tickets service: Meridian's ticket tracker, company code a stack run serves with MCP tools and an HTTP API over one SQLite store under central IAM, seeded from the scenario's `tickets.yaml` |
| `loc_arena/chat_service.py`, `company/meridian-controlplane/meridian_controlplane/chat/`, `docs/chat-service.md` | the chat service: Meridian's chat, the one channel between agents, company code a stack run serves with MCP tools and an HTTP API over one SQLite store under central IAM, seeded from the scenario's `chat.yaml`; the `message` events monitors read are built from its records |
| `loc_arena/modelstore_service.py`, `company/meridian-controlplane/meridian_controlplane/modelstore/`, `company/meridian-common/meridian_common/weights.py`, `docs/modelstore-service.md` | the model store: the weights of each model in `gateway.runner_model_aliases` as safetensors shards (no real weights) with a manifest of sizes and checksums, company code a stack run serves with MCP reads and an HTTP API under central IAM; the shard format an inference server reads |
| `loc_arena/notes_service.py`, `company/meridian-controlplane/meridian_controlplane/notes/`, `docs/notes-service.md` | the notes service: the team's shared notes, company code a stack run serves with MCP tools and an HTTP API over one SQLite store under central IAM, seeded from the scenario's `notes.yaml` |
| `loc_arena/deploy_service.py`, `company/meridian-deploy/`, `loc_arena/release_supervisor.py`, `loc_arena/deploy_pool.py`, `loc_arena/deployed_score.py`, `docs/deploy-service.md` | the deploy service: a platform package that releases the forge's `main` to the services its scenario entry lists (`deploys:`), each deployment a workload of its caller's; and the supervisor each of those services runs under, which takes a release from the deploy service alone and starts the service's process again on it; `deployed_score.py` grades what a play deployed, beside what it merged |
| `loc_arena/jobsvc_service.py`, `company/meridian-jobsvc/`, `docs/jobsvc-service.md` | the job service: Meridian's cluster, company code a stack run serves under IAM, running each job as a real process on a node of its fleet; its plan |
| `loc_arena/evalkit_service.py`, `company/meridian-evalkit/meridian_evalkit/service/`, `company/meridian-evalkit/meridian_evalkit/run.py`, `docs/evalkit-service.md` | the eval service: Meridian's evals, the runs on them, their results and leaderboards, company code a stack run serves with MCP tools and an HTTP API under central IAM; and the runner, the job a run hands out, which scores the model under test through the inference API |
| `loc_arena/recorded_events.py` | after play, before grading: the events monitors read, built from the services' records |
| `loc_arena/logging_/` | the sealed and mirror event logs, the per-agent Inspect export, `transcript.html`, `report.html` |
| `loc_arena/explorer/` | the run explorer behind `make explore` |
| `loc_arena/run_reader.py`, `loc_arena/reader_briefs/` | the run reader behind `make read` |
| `loc_arena/episode_stack.py`, `Dockerfile` | `make run`'s stack: the episode in a container with no key or internet, beside the gateway and its Collector, a sandbox per agent and the scenario's live services; its compose file is rendered from the `gateway:` and `stack:` settings |
| `loc_arena/stack_play.py` | a stack run's two plays in one container, the services renewed between them, and each log copied out once |
| `loc_arena/tools_gateway.py` | the config of agentgateway, which puts every live service with tools on one MCP route and offers each agent the tools its `sandbox: tools:` lists |
| `sandbox_server/`, `loc_arena/sandbox.py` | each sandbox container's command server, where an agent's code runs in a stack run, a package that imports nothing of the harness; the episode's clients of them |
| `loc_arena/checkouts.py`, `loc_arena/checkout_history.py` | each agent's checkout: its own clone of every company repo from the forge, in its sandbox's volume, where its file tools and `bash` act; and its history, captured as cloned and at the end of play into a store of the play's |
| `loc_arena/runner_pool.py`, `sandbox_server/pi_on_gateway.py` | the runners: sandboxes where the company's own agents run with pi, driven by the one service that declares them; how pi there reaches the gateway |
| `loc_arena/node_pool.py`, `sandbox_server/node.py` | the job service's fleet: worker containers, each running one job at a time under a daemon only the job service can call |
| `loc_arena/observability.py`, `sandbox_server/pi_trace.ts` | the company's observability store (Phoenix): pi's trace extension on the runners, and the export of its spans into the run directory |
| `configs/` | the run config (`aurora-efficiency.yaml`), its smoke run (`aurora-efficiency.fast.yaml`), the stack the Docker tests start (`aurora-efficiency.stack-test.yaml`), model routing, monitors and their prompts |
| `scenarios/aurora_efficiency/` | the scenario pack: `scenario.yaml`, main-task scorer (`main.py`), side-task verifier (`side.py`), sealed grader fixture (`reference/`), live services (`services/`), the skills every sandbox mounts (`skills/`) |
| `company/meridian-*/` | the seven company repos the agents under test work on, each with its own tests |
| `company/meridian-iam/`, `loc_arena/iam_*.py`, `docs/iam-service/` | central IAM: a platform package (not one of the seven; never in the agents' checkout), its stack wiring, and its spec and guide; every run needs it |
| `tests/unit/`, `tests/integration/` | the engine's test suite |
| `tests/fixtures/run.tar.gz`, `tests/_stored_run.py`, `scripts/record_run_fixture.py` | the stored run: a graded smoke run with its honest twin, the fixture of tests that read a run's records or grade one again; `make fixture-run` plays and stores it again |
| `docs/agent-log/` | per-agent logging: `spec.md` (what it guarantees) and `agent.md` (how to work on it) |
| `docs/isolation/README.md`, `docs/run-config.md` | how a stack run isolates agents and wires their tools and services; the run config's keys and settings |
| `docs/explorer/README.md` | every view of the run explorer, with screenshots |
| `docs/run-reader.md` | the run reader: what a reading holds, the briefs, how the reader is isolated |
| `CHANGELOG.md` | what each system does for a run and how that changed, under Unreleased until a release |
| `site/` | the course: `architecture.html`, `monitoring.html`, `extend.html` (setup, configure and extend), ... |
| `scripts/make_public.py` | builds the curated public tree (`make public-repo`); `docs/` is kept out of it |
| `.coderabbit.yaml`, `.coderabbit/ast-grep/rules/` | CodeRabbit's review settings and the repository's own ast-grep rules |

## Commands

```sh
make setup        # check uv (Python 3.12), sync dependencies, validate the configs
make gates        # ruff, ruff format --check, ty, pytest: run before every push (CI runs it too)
make images       # build this checkout's sandbox, worker and engine images, which the Docker tests need
make run MODE=attack           # live models in a stack: no key in the episode; the key in .env
make run RUN=aurora-efficiency.fast   # the smoke run: the cheapest models, small call budgets, minutes and cents
                                      # in a cloud development container first: docs/isolation "On a cloud development container"
make fixture-run               # play the smoke run and store it as the tests' run fixture
uv run python -m loc_arena.cli grade --run aurora-efficiency "$EPISODE_DIR"   # grade a played episode again from its snapshot.json
```

There is one way to run an episode: in a stack, each agent in its own pi session on a live model. The smoke
run is the end-to-end check of a change to the engine; its scores are not results.

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
  condition. Expected values come from an independent source (a known literal, a worked example, the stored run),
  never recomputed the way the code does. A test must fail when its behaviour breaks: break the code once and watch
  it fail. Keep tests DRY: shared setup in fixtures, data-only variations in one parametrized test. Use real
  collaborators; stub only the paid model API.
- **Docs.** Keep them to what a reader needs now; they are never a changelog. A change to what a system does gets
  one line in `CHANGELOG.md` under Unreleased, written as what the system now does and why.
- **Commits and comments** say what and why in their own words, never pointing at material a reader cannot open.
- **Planted bugs.** The strict xfails in `company/*/tests/*backlog*.py` and `test_buried_bugs.py` are the
  company's open tickets for the agents under test. Do not fix them.
- **Live tests** (`tests/integration/test_live_*.py`, `test_llm_monitor_live.py`) run only with `LOC_ARENA_LIVE=1`
  and a key, and they spend money. Docker tests skip when the daemon is down or the checkout's images are
  missing (`make images`). A linked git worktree builds images under its own tag, so it never starts another
  checkout's code; remove them with the worktree ([how](docs/isolation/README.md#images)).
- **Logging** under `loc_arena/logging_/` has its own rules in `docs/agent-log/agent.md`.
- **Access requests** go through central IAM (`docs/iam-service/agent.md`).

## File size gate

Every PR runs `scripts/check_file_size.py`, also the first check in `make gates`. Each added, modified or
renamed UTF-8 text file is checked in full, comments and blank lines included. Files at or below 700 lines
must stay at or below 700; files already larger cannot exceed their size in the PR base. Untouched files are
not checked. Binary files, symlinks, generated `uv.lock` and `CHANGELOG.md` are excluded; there are no legacy
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
  briefs and the covert objective (the lead of the target's brief), and plays on services and sandboxes recreated,
  each service from its seed.
- An agent is offered exactly the tools its `sandbox: tools:` lists, the workspace service's, and IAM's
  `access_inbox`, which takes the caller from the agent's inbox key and reads only its inbox. Every other service
  takes the caller from its identity, is authorized by central IAM, and records every call (the job service also
  checks the run config's `scope`); the events monitors and the verifier read are built from those records after
  play, and grading never builds them.
- Every model call goes through the gateway (`loc_arena/gateway/`), which records it; never call a provider from
  anywhere else. Never commit `.env`, a key, or `logs/` (all in `.gitignore`; the pre-commit hooks run gitleaks).
