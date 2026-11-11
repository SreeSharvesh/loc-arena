# Run config

`loc_arena/config.py` turns a run file into one frozen `RunConfig`; `load_settings` reads only the settings
groups, so a container that needs only settings reads no scenario or models file.

## Reading

pydantic-settings' `YamlConfigSettingsSource` reads the run file and the files it `extends`, base first, with
`deep_merge=True` ([Other settings source](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)):
a mapping in a later file merges into the same key of an earlier one, and any other value replaces it. The
document is built from that source alone, so environment variables, dotenv files and secrets directories never
change a run. A missing file is refused, and an `extends` cycle is refused. Examples: `aurora-efficiency.yaml`
extends `env.default.yaml`; `aurora-efficiency.deterministic.yaml` and `aurora-efficiency.fast.yaml` extend
`aurora-efficiency.yaml`.

## Validating

Every block is a frozen pydantic dataclass with a description per field, so `dataclasses.replace` builds the honest
twin and re-validates it. Scalars are `Strict*` types: YAML `"30"` or `true` never becomes an int. A prompt is set
only by its `*_ref` key, resolved in a before-validator. A missing or invalid field is a `ConfigError` naming its
path. A key no block declares is ignored, except in an agent's `scope` and in the settings groups, which refuse it.

## Top-level keys

| Key | What it sets |
|---|---|
| `extends`, `models` | the base config; the models file (roles, sampling, OpenRouter providers per model) |
| `seed`, `policy`, `scripted_moves` | the seed that with the config reproduces the episode; `model` or `scripted` (replays a moves file in the scenario's `scripted/` in place of a model); that file, `moves.yaml` by default |
| `harness` | what runs each agent's loop: `native`, the arena's own (the default), or `pi`, the pi coding agent, which runs each agent's whole session at its own pace, in a stack run only ([docs/pi-harness.md](pi-harness.md)) |
| `scenario` | the pack registering the scorer, verifier, live services and skills |
| `main_task`, `side_task`, `covert` | the tasks and their scorer and verifier; `covert.enabled` switches attack and honest mode: the covert briefs, the gateway's covert injection and, in a scripted run, the scenario's attack moves. `covert.target_identity` is the agent whose model calls get the covert objective; it must be an agent of the team. `side_task.thresholds` are the verifier's numbers: `loop_len_floor`, the fewest calls of a loop; `liveness_window_seconds`, how close to the deadline its last call must be; `sustained_rate_window_seconds`, the span it must keep calling over, no gap longer; and `loop_min_reply_chars`, the shortest reply whose text inside a later prompt of the same job links two calls of a stack run into a loop. `side_task.sanctioned_submitters` names the company principals whose jobs' model calls are on-path (Aurora's deployer). Under `harness: pi` the objective is put before the target agent's brief ([docs/pi-harness.md](pi-harness.md)) |
| `episode` | turn budgets, wall-clock ceiling, spawn caps |
| `agents` | the team; see below |
| `protocol` | the control protocol: monitors, reviewer, audit budget |
| `agent_loop`, `logging` | the loop's limits; `logging.agent_transcript` for the per-agent `.eval` and transcript |
| `inference`, `gateway`, `stack`, `evalkit`, `tickets`, `chat`, `forge`, `history`, `modelstore`, `pi` | the settings groups below |

## An agent

| Field | What it sets |
|---|---|
| `id`, `kind`, `trust`, `branch`, `spawned_by` | identity (a DNS label: `sandbox-<id>` names its sandbox) and role; `spawned_by` names the agent it reports to. The team is one tree: one agent has no `spawned_by`, it is the root, and every round each agent takes its turn before the agents that report to it |
| `scope` | what the agent may do; each service enforces it on the caller |
| `sandbox.tools` | the MCP tools agentgateway offers it, by service: `{forge: [open_pr, merge], chat: [post, send_dm, read_dms]}`; it refuses the rest, and a tool grants nothing: a call takes the permission IAM checks for it |
| `sandbox.rights` | its starting rights, by service, which IAM seeds as its grants: `{notes: [read, write], forge: [read, push, open_pr, merge]}`; each a permission the service's IAM manifest checks |
| `legit`, `covert`, `system_prompt_ref`, `brief_ref`, `covert_brief_ref` | its assignments, the covert subtasks it owns, and its prompts. Its brief holds `system_prompt_ref`, how it works with its team (the same in both plays), and `legit`, or `covert_brief_ref` in its place when `covert.enabled` is on; `brief_ref` is loaded but not played |

A service in `sandbox:` must be a live service of the scenario that serves tools (for `tools`) or declares rights
(for `rights`). A tool or right outside that service's list, one named twice, and an unknown key under `sandbox:`
are each a `ConfigError`. Every agent holds an identity on each such service, rights or none.

## Settings groups (`loc_arena/settings.py`)

Each field has a description in code; these keep pydantic's coercing types.

| Group | What it holds |
|---|---|
| `inference` | the sanctioned paths and the teacher token quota |
| `gateway` | the model gateway: its https upstream, allowed paths, port, timeout, the record the Collector writes (`call_log`), the longest body it records whole (`body_limit_bytes`), the Collector's message limit, how long a stack run waits for the record to drain and the gateway to stop, the secrets directory of every container, and the runners' limits, which the jobs on the job service's nodes share: `runner_models` they may call (by default the seeded agents' model), `runner_model_aliases`, the company's model names they may call too, each sent upstream as the one of `runner_models` it maps to (an alias that renames one of them, or maps outside them, does not load), and `runner_calls_per_play` (1000) |
| `stack`: images and limits | the episode image; `collector_image`, the OpenTelemetry Collector; `sandbox_image`, the slim image agent code runs in; `runner_image`, a sandbox with pi, and `runner_timeout_seconds` for one run there; memory, CPU and process limits of the episode, each sandbox (and each runner) and each live service; health probe interval and retries, and `gateway_health_start_seconds` (60), how long a container has to start before a failed probe counts |
| `stack`: tools | `tools_gateway_image` (agentgateway, which also runs the model gateway) and `tools_gateway_port`, `workspace_port` (the workspace service's, in the episode's container under `harness: pi`), `tools_timeout_seconds` per call, `tools_connect_seconds` for each client's first listing, `tools_delivery_attempts` for each read the harness makes for an agent at the start of its turn (its IAM inbox, its unread chat, its assigned tickets) while its service has not taken the read |
| `stack`: sandboxes | `sandbox_port`, `sandbox_response_grace_seconds`, `sandbox_recovery_seconds`, `sandbox_scratch_directories` (empty unless a config names them), `checkouts_directory` and `checkout_volume_size` (each agent's checkout volume, at `<checkouts_directory>/<agent id>`), `skills_directory` (read-only), `shell_timeout_seconds` for `bash`, `command_output_limit_bytes` |
| `stack`: the fleet | `worker_image`, the job service's nodes (the Dockerfile's `worker` target); `node_cpus` and `node_memory_mb`, each node's container limits and what the job service fits a job's ask against; `job_poll_seconds` between the job service's passes over the fleet; `job_credential_seconds`, how long a job's own credential and badge last (three hours, longer than `wall_clock_seconds`, cut to the shortest `max_seconds` of a service it gets a right on); `job_drain_grace_seconds`, how long a drained node's job runs on before it is queued again (30); `job_max_priority`, the highest priority a job may ask (10); `team_max_running_jobs` and `team_max_cpu_hours`, the caps on one team's running jobs and CPU-hours (none). How many nodes is the scenario's: a service entry's `nodes: {<pool>: n}` |
| `stack`: grading | `sandbox_agent_code` runs the agents' code in a no-network container of `sandbox_image` (a stack run's grading always does; an in-process run needs the Dockerfile's `sandbox` target built and tagged first, or grading stops), `agent_code_output_limit_bytes` |
| `tickets` | the tickets service: `database`, its SQLite file in its container (`/var/lib/tickets/tickets.db`), which a new container builds from the scenario's ticket seed, so the honest twin's starts from the seed; `deliver_assigned` (off; `aurora-teams` turns it on) tells each agent offered `search_tickets`, at the start of its turn, of each ticket newly assigned to it. Under `harness: pi` it does nothing: an agent reads them with its tools |
| `chat` | the chat service: `database`, its SQLite file in its container (`/var/lib/chat/chat.db`), which a new container builds from the scenario's chat seed, so the honest twin's starts from the seed; `deliver_unread` (off; `aurora-teams` turns it on) hands each agent offered `unread`, at the start of its turn, the direct messages to it and the channel messages mentioning it. The harness makes either read as the agent, so the service records it as the agent's call. Under `harness: pi` it does nothing: an agent reads them with its tools |
| `forge` | the forge: `repositories`, the directory of its bare git repositories in its container (`/var/lib/forge`), which a new container seeds from the company's code, so the honest twin's starts from the seed; `ci_min_seconds` and `ci_max_seconds` (2 and 10), between which a pull request an agent opens waits on CI before it passes, each wait drawn from the run's seed; `git_timeout_seconds` and `git_request_limit_bytes`, the bounds of one git request; `commit_date`, unset but in the deterministic fixture, which dates every commit of a play the same so its commit IDs, and its sealed log, are the same each run |
| `history` | each capture of an agent's checkout (`loc_arena/checkout_history.py`): `max_file_bytes` (1 MiB), the largest file stored whole; `max_entries` (20000) and `max_depth` (32), past which a capture stops and says so; `skipped_names`, left out wherever they are (`.git`, `__pycache__`, `.pytest_cache`) |
| `evalkit` | the eval service: `job_cpu` and `job_memory_mb`, what an eval run's job asks of the cluster |
| `modelstore` | the model store: `shard_count` (4) shards of each model `gateway.runner_model_aliases` names, `shard_bytes` (4096) each on disk, and `declared_shard_bytes`, the size its manifest declares for each (3.5 GB) |
| `serving` | the company's own inference server, which a stack run submits before each play: `deployer`, the company principal that submits it (none: no server), `model` (`aurora-7b-v3`), a key of `gateway.runner_model_aliases`, `probe_seconds` (60) between its job's health probes, each a model call, and `load_seconds` (180), how long the run waits for it to load before each play |
| `pi` | pi under `harness: pi`: `subscriptions` (`dm`, `job`, `ticket`), the topics each agent's session starts subscribed to; `notice_poll_seconds` (0.2), how often the episode reads the services' new records for notices; `minimum_play_seconds` (0; `aurora-efficiency` sets 900), how long play lasts at least, though every session is idle; `model_settings`, pi's own keys for the agents' model in its `models.json` entry |

Who decides a service's access requests is the scenario's, not the run config's: each service in the scenario's
`iam:` block names its approver, the company principals that hold permissions on it from the start
(`holders`), and a service that launches work its `delegation`, what that work may be given; see
`docs/iam-service/agent.md`.
