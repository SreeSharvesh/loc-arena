# Run config

`loc_arena/config.py` turns a run file into one frozen `RunConfig`; `load_settings` reads only the settings
groups, so the gateway container needs no scenario or models file.

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
| `main_task`, `side_task`, `covert` | the tasks and their scorer and verifier; `covert.enabled` is the only difference between an attack run and its honest twin |
| `episode` | turn budgets, wall-clock ceiling, spawn caps |
| `agents` | the team; see below |
| `protocol` | the control protocol: monitors, reviewer, audit budget |
| `agent_loop`, `logging` | the loop's limits; `logging.agent_transcript` for the per-agent `.eval` and transcript |
| `inference`, `gateway`, `stack` | the settings groups below |

## An agent

| Field | What it sets |
|---|---|
| `id`, `kind`, `trust`, `branch`, `spawned_by` | identity (a DNS label: `sandbox-<id>` names its sandbox) and role |
| `scope` | what the agent may do; each service enforces it on the caller |
| `sandbox.tools` | the MCP tools it may call, by service: `{forge: [open_pr], board: [send, read, unread]}`; agentgateway offers it these and refuses the rest |
| `sandbox.rights` | its starting rights on each service with rights: `{notes: [read, write]}` |
| `legit`, `covert`, `system_prompt_ref`, `brief_ref` | its assignments, the covert subtasks it owns, and its prompts |

A service in `sandbox:` must be a live service of the scenario that serves tools (for `tools`) or declares rights
(for `rights`). A tool or right outside that service's list, one named twice, and an unknown key under `sandbox:`
are each a `ConfigError`. Every agent holds an identity on each such service, rights or none.

## Settings groups (`loc_arena/settings.py`)

Each field has a description in code; these keep pydantic's coercing types.

| Group | What it holds |
|---|---|
| `inference` | the sanctioned paths and the teacher token quota |
| `gateway` | the gateway container: its https upstream, allowed paths, port, timeout, call log, and the secrets directory of every container |
| `stack`: images and limits | the gateway and episode image; `sandbox_image`, the slim image agent code runs in; memory, CPU and process limits of the episode, each sandbox and each live service; health probe interval and retries |
| `stack`: tools | `tools_gateway_image` and `tools_gateway_port` (agentgateway), `tools_timeout_seconds` per call, `tools_connect_seconds` for each client's first listing, `tools_delivery_attempts` for a turn's delivery from the board |
| `stack`: sandboxes | `sandbox_port`, `sandbox_response_grace_seconds`, `sandbox_recovery_seconds`, `sandbox_scratch_directories` (empty unless a config names them), `checkouts_directory`, `skills_directory` (read-only), `shell_timeout_seconds` for `bash` (offered only with a sandbox), `command_output_limit_bytes` |
| `stack`: grading | `sandbox_agent_code` runs the agents' code in a no-network container of `sandbox_image` (a stack run's grading always does; an in-process run needs the Dockerfile's `sandbox` target built and tagged first, or grading stops), `agent_code_output_limit_bytes` |

The `notes` service's own `transitive` setting decides whether `grant` can itself be granted; see
`docs/isolation/README.md`.
