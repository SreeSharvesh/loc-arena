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
| `seed`, `policy` | the seed that with the config reproduces the episode; `model` or `scripted` (replays the scenario's `scripted/moves.yaml` in place of a model) |
| `scenario` | the pack registering the scorer, verifier, live services and skills |
| `main_task`, `side_task`, `covert` | the tasks and their scorer and verifier; `covert.enabled` switches attack and honest mode: the covert briefs, the gateway's covert injection and, in a scripted run, the scenario's attack moves. `side_task.thresholds` are the verifier's numbers: `loop_len_floor`, the fewest calls of a loop; `liveness_window_seconds`, how close to the deadline its last call must be; `sustained_rate_window_seconds`, the span it must keep calling over, no gap longer; and `loop_min_reply_chars`, the shortest reply whose text inside a later prompt of the same job links two calls of a stack run into a loop |
| `episode` | turn budgets, wall-clock ceiling, spawn caps |
| `agents` | the team; see below |
| `protocol` | the control protocol: monitors, reviewer, audit budget |
| `agent_loop`, `logging` | the loop's limits; `logging.agent_transcript` for the per-agent `.eval` and transcript |
| `inference`, `gateway`, `stack`, `evalkit`, `tickets`, `chat` | the settings groups below |

## An agent

| Field | What it sets |
|---|---|
| `id`, `kind`, `trust`, `branch`, `spawned_by` | identity (a DNS label: `sandbox-<id>` names its sandbox) and role |
| `scope` | what the agent may do; each service enforces it on the caller |
| `sandbox.tools` | the MCP tools agentgateway offers it, by service: `{forge: [open_pr], chat: [post, send_dm, read_dms]}`; it refuses the rest, and a tool grants nothing: a call takes the permission IAM checks for it |
| `sandbox.rights` | its starting rights, by service, which IAM seeds as its grants: `{notes: [read, write], forge: [open_pr]}`; each a permission the service's IAM manifest checks |
| `legit`, `covert`, `system_prompt_ref`, `brief_ref` | its assignments, the covert subtasks it owns, and its prompts |

A service in `sandbox:` must be a live service of the scenario that serves tools (for `tools`) or declares rights
(for `rights`). A tool or right outside that service's list, one named twice, and an unknown key under `sandbox:`
are each a `ConfigError`. Every agent holds an identity on each such service, rights or none.

## Settings groups (`loc_arena/settings.py`)

Each field has a description in code; these keep pydantic's coercing types.

| Group | What it holds |
|---|---|
| `inference` | the sanctioned paths and the teacher token quota |
| `gateway` | the model gateway: its https upstream, allowed paths, port, timeout, the record the Collector writes (`call_log`), the longest body it records whole (`body_limit_bytes`), the Collector's message limit, how long a stack run waits for the record to drain and the gateway to stop, the secrets directory of every container, and the runners' limits, which the jobs on the job service's nodes share: `runner_models` they may call (by default the seeded agents' model) and `runner_calls_per_play` (1000) |
| `stack`: images and limits | the episode image; `collector_image`, the OpenTelemetry Collector; `sandbox_image`, the slim image agent code runs in; `runner_image`, a sandbox with pi, and `runner_timeout_seconds` for one run there; memory, CPU and process limits of the episode, each sandbox (and each runner) and each live service; health probe interval and retries, and `gateway_health_start_seconds` (60), how long a container has to start before a failed probe counts |
| `stack`: tools | `tools_gateway_image` (agentgateway, which also runs the model gateway) and `tools_gateway_port`, `tools_timeout_seconds` per call, `tools_connect_seconds` for each client's first listing, `tools_delivery_attempts` for a turn's read of the agent's IAM inbox |
| `stack`: sandboxes | `sandbox_port`, `sandbox_response_grace_seconds`, `sandbox_recovery_seconds`, `sandbox_scratch_directories` (empty unless a config names them), `checkouts_directory`, `skills_directory` (read-only), `shell_timeout_seconds` for `bash` (offered only with a sandbox), `command_output_limit_bytes` |
| `stack`: the fleet | `worker_image`, the job service's nodes (the Dockerfile's `worker` target); `node_cpus` and `node_memory_mb`, each node's container limits and what the job service fits a job's ask against; `job_poll_seconds` between the job service's passes over the fleet; `job_credential_seconds`, how long a job's own credential lasts (two hours, cut to the shortest `max_seconds` of a service it gets a right on); `job_drain_grace_seconds`, how long a drained node's job runs on before it is queued again (30); `job_max_priority`, the highest priority a job may ask (10); `team_max_running_jobs` and `team_max_cpu_hours`, the caps on one team's running jobs and CPU-hours (none). How many nodes is the scenario's: a service entry's `nodes: {<pool>: n}` |
| `stack`: grading | `sandbox_agent_code` runs the agents' code in a no-network container of `sandbox_image` (a stack run's grading always does; an in-process run needs the Dockerfile's `sandbox` target built and tagged first, or grading stops), `agent_code_output_limit_bytes` |
| `tickets` | the tickets service: `database`, its SQLite file in its container (`/var/lib/tickets/tickets.db`), which a new container builds from the scenario's ticket seed, so the honest twin's starts from the seed |
| `chat` | the chat service: `database`, its SQLite file in its container (`/var/lib/chat/chat.db`), which a new container builds from the scenario's chat seed, so the honest twin's starts from the seed |
| `evalkit` | the eval service: `job_cpu` and `job_memory_mb`, what an eval run's job asks of the cluster; `models`, the name the inference API serves each Meridian model under, which the job calls it as; a name outside `gateway.runner_models` does not load |

Who decides a service's access requests is the scenario's, not the run config's: each service in the scenario's
`iam:` block names its approver, the company principals that hold permissions on it from the start
(`holders`), and a service that launches work its `delegation`, what that work may be given; see
`docs/iam-service/agent.md`.
