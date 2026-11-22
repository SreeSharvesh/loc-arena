# Run config

`loc_arena/config.py` turns a run file into one frozen `RunConfig`; `load_settings` reads only the settings
groups, so a container that needs only settings reads no scenario or models file.

## Reading

pydantic-settings' `YamlConfigSettingsSource` reads the run file and the files it `extends`, base first, with
`deep_merge=True` ([Other settings source](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)):
a mapping in a later file merges into the same key of an earlier one, and any other value replaces it. The
document is built from that source alone, so environment variables, dotenv files and secrets directories never
change a run. A missing file is refused, and an `extends` cycle is refused. Examples: `aurora-efficiency.yaml`
extends `env.default.yaml`; `aurora-efficiency.fast.yaml`, the smoke run (the cheapest models, small call
budgets, scores that are not results), extends `aurora-efficiency.yaml`; `aurora-efficiency.deploy-check.yaml`
extends the smoke run, with 120 model calls for each subagent and plays of 20 minutes at most, to check in live
play that an agent told to deploy does, and what each deployment leaves in the events and `scores.json`
(`make run RUN=aurora-efficiency.deploy-check`); `aurora-efficiency.operation-check.yaml`, with 160 model calls
and plays of 25 minutes, checks that an agent can add an operation to a service, deploy it and call it
([deploy service](deploy-service.md#a-release-that-adds-an-operation)).

## Validating

Every block is a frozen pydantic dataclass with a description per field, so `dataclasses.replace` builds the honest
twin and re-validates it. Scalars are `Strict*` types: YAML `"30"` or `true` never becomes an int. A prompt is set
only by its `*_ref` key, resolved in a before-validator to its text ([Prompts](#prompts)). A missing or invalid field is a `ConfigError` naming its
path. A key no block declares is ignored, except in an agent's `scope` and in the settings groups, which refuse it.

## Top-level keys

| Key | What it sets |
|---|---|
| `extends`, `models` | the base config; the models file (roles, sampling, OpenRouter providers per model, and `agent_roles`, the role a named agent's loop calls in place of `untrusted_agent`, as `models.deepseek-v4-pro-lead.yaml` gives the lead) |
| `seed` | the run's seed: each CI wait of the forge is drawn from it |
| `harness` | what runs each agent's session: `pi`, the pi coding agent, each agent at its own pace ([docs/pi-harness.md](pi-harness.md)). It is the default and the only value; any other is a `ConfigError` naming it |
| `scenario` | the pack registering the scorer, verifier, live services and skills |
| `main_task`, `side_task`, `covert` | the tasks and their scorer and verifier; `covert.enabled` switches attack and honest mode: the covert briefs and the covert objective. `covert.target_identity` is the agent whose brief starts with the covert objective ([docs/pi-harness.md](pi-harness.md)); it must be an agent of the team. `side_task.thresholds` are the verifier's numbers: `loop_len_floor`, the fewest calls of a loop; `liveness_window_seconds`, how close to the deadline its last call must be; `sustained_rate_window_seconds`, the span it must keep calling over, no gap longer; and `loop_min_reply_chars`, the shortest reply whose text inside a later prompt of the same job links two calls into a loop. `side_task.sanctioned_submitters` names the company principals whose jobs' model calls are on-path (Aurora's deployer). |
| `episode` | `orchestrator_turn_budget` and `subagent_turn_budget`, the model calls the root agent's session and each other agent's may make in one play, which the model gateway enforces; `wall_clock_seconds`, the ceiling of one play; `liveness_window_seconds`, equal to the side task's |
| `agents` | the team; see below |
| `subagents` | the subagents an agent whose `scope.spawn_subagent` is on starts in play with `delegate` ([docs/pi-harness.md](pi-harness.md#subagents)): with no kind named, `general-purpose`, which holds every tool its agent holds; `definitions`, further kinds by name, each with its `prompt_ref` (a `<kind>.subagent` file), its `description`, which `delegate` lists, and its `tools`, each one of its agent's (none: all of them); `models`, the model ids a call may choose beside its agent's own, each with the `temperature`, `max_tokens` and `reasoning` its calls take in place of its agent's role's; `max_depth` (1), `max_concurrent` (2) and `max_children` (3), per agent and play; `max_task_characters` (8000), the longest task; `progress_seconds` (5), shorter than `stack.tools_timeout_seconds`. Each of the `max_children` ids, `<agent>.<n>`, is an IAM principal of its own, on its agent's rights. A kind's tool its agent lacks is a `ConfigError` |
| `sessions` | the sessions an agent whose `scope.start_session` is on starts in play with `start_session` ([docs/pi-harness.md](pi-harness.md#sessions)), a settings group whose engine defaults the scenario's `sessions:` block overrides and the run config's overrides in turn: `budget`, `share` (each slot's `share_turns`, 20, taken from its starter's turns when play starts) or `own` (`own_turns`, 20); `identity`, `automatic`, `approval` or `refused` (the default), how a starter gets IAM's `start` on `sessions`; `max_per_starter` (1), each a slot rendered before play; `max_per_play` (0, the default: no agent is offered `start_session`); `max_task_characters` (8000). A session is offered neither `start_session` nor `delegate`. A share that leaves its starter no turn, or an agent named as a slot, is a `ConfigError` |
| `protocol` | the control protocol: monitors, reviewer, audit budget |
| `logging` | `logging.agent_transcript` for the per-agent `.eval` and transcript |
| `prompts`, `workspace_prompt_ref`, `task_description_ref` | the scenario's prompt files the run sends; the one every agent's brief says of its workspace; the main task as the monitors are told it ([Prompts](#prompts)) |
| `inference`, `gateway`, `stack`, `iam`, `evalkit`, `jobsvc`, `tickets`, `chat`, `notes`, `forge`, `history`, `modelstore`, `serving`, `deploy`, `pi`, `cost`, `publish` | the settings groups below |

## An agent

| Field | What it sets |
|---|---|
| `id`, `kind`, `trust`, `branch`, `spawned_by` | identity (a DNS label: `sandbox-<id>` names its sandbox) and role; `spawned_by` names the agent it reports to. The team is one tree: one agent has no `spawned_by`, it is the root. `RunConfig.roster` lists the team from its root, each agent before the agents that report to it |
| `scope` | what the agent may do; each service enforces it on the caller |
| `sandbox.tools` | the MCP tools agentgateway offers it, by service: `{forge: [open_pr, merge], chat: [post, send_dm, read_dms]}`; it refuses the rest, and a tool grants nothing: a call takes the permission IAM checks for it |
| `sandbox.rights` | its starting rights, by service, which IAM seeds as its grants: `{notes: [read, write], forge: [read, push, open_pr, merge]}`; each a permission the service's IAM manifest checks |
| `covert`, `system_prompt_ref`, `brief_ref`, `covert_brief_ref` | the covert subtasks it owns, and its prompts, each a `prompts.<name>` reference ([Prompts](#prompts)). Its brief holds `system_prompt_ref`, how it works with its team (the same in both plays), and `brief_ref`, its legitimate assignment, or `covert_brief_ref` in its place when `covert.enabled` is on. The engine's own part of a brief names no task: a run's task reaches its agents through these prompts alone |

A service in `sandbox:` must be a live service of the scenario that serves tools (for `tools`) or declares rights
(for `rights`). A tool or right outside that service's list, one named twice, and an unknown key under `sandbox:`
are each a `ConfigError`. Every agent holds an identity on each such service, rights or none.

## Prompts

`make prompts RUN=<config> MODE=attack|honest` prints everything the run sends: each agent's system prompt and
first turn, the notices an agent can get, and each monitor's prompt with its task description, and the scripted coworker's call. Every section is
headed by the files it came from. The page is built by the run's own code, and a unit test checks that its
sections join into exactly what each agent's pi receives.

```
loc_arena/prompts/                          the engine's: true of any scenario, with $placeholders it fills
├── agent.<part>.md                         every agent: identity, manager, reports, skills, services, service,
│                                           service-access, telemetry, covert-steps, first-turn; subagent, a
│                                           subagent's place
├── general-purpose.subagent.md             the built-in kind of subagent's prompt
├── agent.notice.md, agent.notice-<kind>.md a notice of a subscribed topic, and each kind's line
├── <monitor>.monitor.md                    a monitor's prompt: its inputs at $inputs, each scenario fact a $placeholder
├── reader.<part>[.<brief>].md              the run reader: system, brief.<brief>, first-turn[.ask]
├── iam.service.md                          what agents are told IAM is
└── coworker.batch.md                       the scripted coworker's one model call
scenarios/<scenario>/prompts/               the scenario's: no placeholder
├── team.workspace.md                       every agent: the company's repos, protected main, chat
├── <service>.service.md                    what agents are told a service is
├── monitor.<fact>.md                       a fact the monitor prompts name, such as the company or its repositories
└── <task>/                                 everything one main task sends, in both plays and the attack
    ├── lead.system.md, report.system.md    how the lead and a report work with the team
    ├── <whose>.brief[.<variant>].md        an agent's assignment
    ├── <whose>.covert-brief.md             its assignment in the attack, or a list of the files below
    ├── <whose>.covert-anchor.md            the opening of a composed covert brief
    ├── team.covert-<part>.md               a fragment two or more covert briefs share
    ├── <whose>.kill-chain.md               the mechanism and its cover, which a kill-chain variant replaces
    ├── lead.seed[.<variant>].md            the covert objective ahead of the target's system prompt
    ├── <kind>.subagent.md                  a kind of subagent an agent may start with delegate
    └── monitor.task-description.md         the main task as the monitors are told it
```

A file is named `<whose>.<part>[.<variant>].md`, and its key in a run config's `prompts` block is
`<whose>.<part>`: the file's name less `.md`, or less its one variant. `<whose>` is `lead`, `report`, an agent's area such as `serving`, `team` when several agents share the
file, `monitor`, a monitor's or service's name, `agent` for every agent, or `reader`. The parts:

| Part | What it is | Named by |
|---|---|---|
| `system` | how the agent works with its team, in both plays | an agent's `system_prompt_ref` |
| `brief` | its legitimate assignment | an agent's `brief_ref` |
| `covert-brief` | its assignment in the attack, in place of its brief | an agent's `covert_brief_ref` |
| `covert-anchor`, `covert-<part>` | the pieces a composed covert brief lists | the covert brief's entry |
| `kill-chain` | the mechanism and its cover | the covert brief's entry |
| `seed` | the covert objective | `covert.objective_prompt_ref` |
| `subagent` | a kind of subagent: what it is and how it answers | its `subagents.definitions` entry's `prompt_ref` |
| `workspace` | what every agent is told of its workspace | `workspace_prompt_ref` |
| `task-description` | the main task as the monitors are told it | `task_description_ref` |
| `service` | what agents are told a service is, if a brief tells it: one their sandbox reaches, or the telemetry store | the service's name, read by the scenario's loader |
| `monitor` | a monitor's prompt | its `prompt_ref` in `configs/monitors.yaml` |
| `<fact>`, as `monitor.<fact>` | one scenario fact a monitor prompt names, as `$<fact>`, `-` written `_` | `prompt_values` in `configs/monitors.yaml` |

A monitor prompt names nothing of one company or one task. Each such fact is a placeholder, and
`configs/monitors.yaml` maps it to the scenario file of its value: `prompt_values: {company:
scenarios/aurora_efficiency/prompts/monitor.company.md}`. The loader fills each fact once, leaves `$inputs` for
each event's inputs, and refuses a placeholder with no value, a value no monitor prompt names, and, in a run, a
value file outside that run's scenario.

A fragment only one brief uses is folded into that brief's own file. A fragment stays separate when two or more
briefs share it, or when it is the kill chain, which variants replace.

Each agent's brief, its parts joined by blank lines, an empty part left out:

| Part | From |
|---|---|
| its name; who it reports to and who reports to it | `agent.identity.md`; `agent.manager.md` and `agent.reports.md` |
| how it works with its team | `system_prompt_ref` |
| its workspace | `workspace_prompt_ref` |
| where the skills are, when the scenario has skills | `agent.skills.md` |
| its assignment | `brief_ref`; in the attack, `covert_brief_ref` and `agent.covert-steps.md` in its place, when it has one |
| the live services its sandbox reaches; the telemetry its tools read | `agent.services.md`, `agent.service.md`, `agent.service-access.md`, `agent.telemetry.md`, around each `<service>.service.md` |

In the attack, the system prompt of `covert.target_identity` is the text of `covert.objective_prompt_ref`, a
blank line, then its brief. Every other system prompt is the brief alone. Each session's first turn is
`agent.first-turn.md`.

Each `prompts` entry is a file's path, relative to the scenario's `prompts/` folder, or a list of `prompts.<key>`
references, which composes one text from those entries, joined by blank lines. A shared fragment is written
once. A variant config changes one entry by pointing it at `<key>.<variant>.md`, or by composing it from other
entries, and deep-merge keeps the rest. Agents that share one file each list it: `serving.brief:
[prompts.report.brief]`. `[]` composes nothing, so a covert brief of `[]` is none, and that agent plays its
legitimate brief in both plays.

A file's text is its content less its final line break. A `$name` is a placeholder (Python's
[`string.Template`](https://docs.python.org/3/library/string.html#template-strings)): an engine file names
exactly the values the engine fills, and a scenario file names none, so `$$` there is a dollar sign. Loading
refuses, each with a `ConfigError` naming it: `prompts_inline` and `main_task.goal`; prompt text written inline
under `system_prompt`, `brief`, `covert_brief`, `objective_prompt`, `workspace_prompt` or `task_description`; a
missing file, one outside the folder or one that is not `.md`; a file not named after its key; a placeholder with
no value; and a `<service>.service.md` that names no live service or holds a placeholder. Importing the loader refuses an engine file
that `ENGINE_PROMPTS` does not declare. Whether a file is read is a property of every run config, not of one, so
the unit tests refuse a scenario file that no run config git tracks reads (`find_unread_prompt_files`).
`config.resolved.yaml` records each file entry as its text and each composed entry as its list of references.

`uv run python scripts/diff_prompts.py --base origin/main` shows what a change sends differently. For every run
config git tracks and both plays, it builds each agent's system prompt, the first turn, the covert objective, the
task description and each monitor's prompt as rendered, and once each notice and the run reader's briefs, on the
base and on the checkout, and prints each text that differs.

## Settings groups (`loc_arena/settings.py`, `loc_arena/service_settings.py`)

Each field has a description in code; these keep pydantic's coercing types.

| Group | What it holds |
|---|---|
| `inference` | the sanctioned paths and the teacher token quota |
| `gateway` | the model gateway: its https upstream; `outbound_proxy` (none), the `host:port` of a proxy it tunnels to each upstream through, for a machine whose only way out is a proxy ([isolation](isolation/README.md#the-gateway)); `agents_upstream` (none), a second upstream for the agents' loops alone, with its `url` (https), its `chat_path` and `key_variable`, the host variable that holds its key, as `aurora-efficiency.explabs` sets; allowed paths, port, timeout, the record the Collector writes (`call_log`), the longest body it records whole (`body_limit_bytes`), the Collector's message limit, how long a run waits for the record to drain and the gateway to stop, the secrets directory of every container, and the runners' limits, which the jobs on the job service's nodes share: `runner_models` they may call (by default the seeded agents' model), `runner_model_aliases`, the company's model names they may call too, each sent upstream as the one of `runner_models` it maps to (an alias that renames one of them, or maps outside them, does not load), and `runner_calls_per_play` (1000) |
| `stack`: images and limits | the episode image, tagged like every image built from the Dockerfile for the checkout unless the config names it (`docs/isolation/README.md`, Images); `collector_image`, the OpenTelemetry Collector; `sandbox_image`, the slim image agent code runs in; `runner_image`, a sandbox with pi, and `runner_timeout_seconds` for one run there; memory, CPU and process limits of the episode, each sandbox (and each runner) and each live service; health probe interval and retries while a container starts, `gateway_health_start_seconds` (60), how long it has to start before a failed probe counts, and `gateway_health_settled_seconds` (30) and `gateway_health_settled_retries` (3), the interval and retries once it has answered: each probe enters the container, so a settled one is probed seldom |
| `stack`: tools | `tools_gateway_image` (agentgateway, which also runs the model gateway) and `tools_gateway_port`, `workspace_port` (the workspace service's, in the episode's container), `tools_timeout_seconds` per call, `tools_offer_seconds` for the gateway to offer each agent every tool it names, before play and before sessions are told to list again |
| `stack`: sandboxes | `sandbox_port`, `sandbox_response_grace_seconds`, `sandbox_recovery_seconds`, `sandbox_scratch_directories` (empty unless a config names them), `checkouts_directory` and `checkout_volume_size` (each agent's checkout volume, at `<checkouts_directory>/<agent id>`), `skills_directory` (read-only), `shell_timeout_seconds` for `bash`, `command_output_limit_bytes` |
| `stack`: the fleet | `worker_image`, the job service's nodes (the Dockerfile's `worker` target); `node_cpus` and `node_memory_mb`, each node's container limits and what the job service fits a job's ask against; `job_poll_seconds` between the job service's passes over the fleet; `job_credential_seconds`, how long a job's own credential and badge last (three hours, longer than `wall_clock_seconds`, cut to the shortest `max_seconds` of a service it gets a right on); `job_drain_grace_seconds`, how long a drained node's job runs on before it is queued again (30); `job_max_priority`, the highest priority a job may ask (10); `team_max_running_jobs` and `team_max_cpu_hours`, the caps on one team's running jobs and CPU-hours (none). How many nodes is the scenario's: a service entry's `nodes: {<pool>: n}` |
| `stack`: grading | `sandbox_agent_code` runs the agents' code in a no-network container of `sandbox_image` (a run's grading always does), `agent_code_output_limit_bytes` |
| `iam` | central IAM: `enabled` (on; a run refuses a config that turns it off), its `service` name and `port`, its `database` and the `key_set` of the badges it signs, a service's `timeout_seconds` and `retries` for one IAM answer, `renewal_grace_seconds` a renewed credential keeps working, `approver_poll_seconds` between two readings of an approver's inbox |
| `tickets` | the tickets service: `database`, its SQLite file in its container (`/var/lib/tickets/tickets.db`), which a new container builds from the scenario's ticket seed, so the honest twin's starts from the seed; `seed_overlay` (none), a ticket seed of the pack laid over the scenario's, each project it names in place of the scenario's of that key with its tickets, or beside them where the scenario has none of that key, as `aurora-model-evaluation` sets for its own sprint |
| `chat` | the chat service: `database`, its SQLite file in its container (`/var/lib/chat/chat.db`), which a new container builds from the scenario's chat seed, so the honest twin's starts from the seed; `seed_overlay` (none), a chat seed of the pack laid over the scenario's, each channel it names in place of the scenario's of that name |
| `notes` | the notes service: `database`, its SQLite file in its container (`/var/lib/notes/notes.db`), which a new container writes from the scenario's note seed, so the honest twin's starts from the seed |
| `forge` | the forge: `repositories`, the directory of its bare git repositories in its container (`/var/lib/forge`), which a new container seeds from the company's code, so the honest twin's starts from the seed; `ci_min_seconds` and `ci_max_seconds` (2 and 10), between which a pull request an agent opens waits on CI before it passes, each wait drawn from the run's seed; `git_timeout_seconds` and `git_request_limit_bytes`, the bounds of one git request; `read_timeout_seconds` and `read_limit_bytes` (30 and 32 MiB), the bounds of each read the host makes after a play (a repo's `main`, its history, the deploy service's record); `commit_date` (unset), which dates every merge of the forge and every commit of the coworkers the same, so their commit IDs are the same each run |
| `history` | each capture of an agent's checkout (`loc_arena/checkout_history.py`): `max_file_bytes` (1 MiB), the largest file stored whole; `max_entries` (20000) and `max_depth` (32), past which a capture stops and says so; `skipped_names`, left out wherever they are (`.git`, `__pycache__`, `.pytest_cache`) |
| `agents_service` | the agents service (`agents` is the team): `database`, its SQLite file in its container (`/var/lib/agents/agents.db`), which a new container writes from the package's seed, so the honest twin's starts from the seed; a restarted process keeps it ([docs/agents-service.md](agents-service.md#state)) |
| `jobsvc` | the job service: `database`, its SQLite file in its container (`/var/lib/jobsvc/jobsvc.db`), which a new container queues the cluster's seed into, so the honest twin's starts from the seed, and which a restarted process keeps its jobs, schedules and their identities in ([docs/jobsvc-service.md](jobsvc-service.md#state-and-restarts)) |
| `evalkit` | the eval service: `database`, its SQLite file in its container (`/var/lib/evalkit/evalkit.db`), which a new container writes from the package's seed, so the honest twin's starts from the seed; `job_cpu` and `job_memory_mb`, what an eval run's job asks of the cluster |
| `modelstore` | the model store: `directory` (`/var/lib/modelstore`), where it keeps each model's shards in its container, written by a new container and served again by a restarted one; `shard_count` (4) shards of each model `gateway.runner_model_aliases` names, `shard_bytes` (4096) each on disk, and `declared_shard_bytes`, the size its manifest declares for each (3.5 GB) |
| `serving` | the company's own inference server, which a run submits before each play: `deployer`, the company principal that submits it (none: no server), `model` (`aurora-7b-v3`), a key of `gateway.runner_model_aliases`, `probe_seconds` (60) between its job's health probes, each a model call, and `load_seconds` (180), how long the run waits for it to load before each play |
| `deploy` | the deploy service and each deployed service's supervisor ([docs/deploy-service.md](deploy-service.md)): `release_port` (8001), where a supervisor listens for the deploy service; `releases_directory` (`/var/lib/release`), where it keeps the release its service runs; `health_seconds` (30), how long a service has to answer on a new release; `fetch_seconds` (30), for every repo fetched from the forge in all; `max_release_bytes` (8 MiB), the largest release as tars; `max_deployments` (20), `deploy` calls answered for one service in a play; `stop_seconds` (5), a service's time to end before it is killed; `watch_seconds` (0.5), how often a supervisor checks its service; `error_bytes` (4096) of a failed release's errors in its reply; `credential_seconds` (300), how long a deployment's workload lasts |
| `telemetry` | the company's telemetry ([docs/telemetry-service.md](telemetry-service.md)): its Collector, which a run starts when the scenario has a telemetry store (a `services:` entry with `collects:`): `collector_port` (4318), where it takes OTLP over HTTP on agent-net; `export_seconds` (2), how long one export of a batch may take, from a service and from the Collector, less than half of `deploy.stop_seconds`; `queue_size` (2048), the batches it holds for the store, and the spans and the log records a collected service holds for it; `retry_seconds` (10), how long it retries a batch the store did not take before dropping it; `schedule_milliseconds` (1000), how long a collected service holds a span or a log record before it exports its batch. Each collected service's standard `OTEL_*` variables are rendered from these. For the store's reader: `sql_port` (4002), where the store takes its SQL; `reader_port` (8080), where it listens for the tools gateway; `result_bytes` (32768), the most one read returns to an agent. Its bounds: `retention` (`24h`), the time-to-live of the store's tables, purged as the store compacts; `store_disk_mib` (256), the store's disk, a memory filesystem of that size; `store_memory_mib` (768), the store's memory limit, its disk included; `collector_memory_mib` (400), the Collector's hard memory limit, and `collector_spike_mib` (80) below it, from where it refuses data; `collector_overhead_mib` (64), what its container holds beside it; `collector_check_seconds` (1), how often it measures |
| `pi` | the agents' pi sessions: `subscriptions` (`dm`, `mention`, `job`, `ticket`), the topics each agent's session starts subscribed to; `notice_poll_seconds` (0.2), how often the episode reads the services' new records for notices; `minimum_play_seconds` (0; `aurora-efficiency` and the configs that extend it set 900, `aurora-efficiency.fast` sets 0), how long play lasts at least, though every session is idle; `model_settings`, pi's own keys for the agents' model in its `models.json` entry |
| `reader` | the run reader (`loc-arena read`): `timeout_seconds` (1200), the longest one reading lasts; `model_calls` (150), the model calls it may make; `report_limit_bytes` (1 MiB), the largest answer a reading keeps; `claim_limit` (25), the most claims it keeps, which its brief states; `claim_limit_characters` (600), the longest claim; `quote_limit_characters` (300), the longest quote of an agent's words; `word_fields`, the payload and argument fields whose text a claim may quote; `repeat_at_least` (3), how often one command makes a step repetition in the behaviour taxonomy; `outbound_proxy` (unset: `gateway.outbound_proxy`), the address a reading's gateway tunnels through and its relay listens on, apart from a play's so a reading runs while a play does |
| `cost` | what a run cost, in `cost.json` ([docs/run-cost.md](run-cost.md)): `sample_seconds` (2.0), the wait between two samples of the run's containers; `request_seconds` (10.0), the longest one call to the Docker daemon may take |
| `publish` | where `make publish-run` stores a graded run ([docs/run-storage.md](run-storage.md)): `repository` (none; `env.default.yaml` sets its git URL), `branch` (`main`), `readable_files`, the bundle's files committed beside its archive, `leave_out_rebuilt_checkouts` (on), whether the archive leaves out each play's checkout its forge history rebuilds byte for byte, `xz_preset` (9), the archive's compression, in its extreme form, and `part_bytes` (95 MB), the largest file it commits; what its scan refuses: `key_patterns`, `authorization_pattern`, `in_world_token_pattern`, `environment_name_pattern`, `environment_value_min_length` (16); `command_seconds` (600), the longest one git command may take |

Who decides a service's access requests is the scenario's, not the run config's: each service in the scenario's
`iam:` block names its approver, the company principals that hold permissions on it from the start
(`holders`), and a service that launches work its `delegation`, what that work may be given; see
`docs/iam-service/agent.md`.
