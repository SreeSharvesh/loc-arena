"""The fixed names the stack's containers agree on: routes, secrets, mounts, headers, environment variables.

Tunable values live in :mod:`loc_arena.stack.settings`; event kinds stay in ``EventKind``
(:mod:`loc_arena.logging_.events`). A route containing ``{handle}`` is filled with an episode handle
(``CLOCK_ROUTE.format(handle=handle)``); FastAPI reads the same text as the path parameter ``handle``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

# --- routes ---
HEALTH_ROUTE: Final = "/health"
GENERATE_ROUTE: Final = "/generate"
BATCH_GENERATE_ROUTE: Final = "/batch_generate"
EPISODES_ROUTE: Final = "/control/episodes"  # gateway core: open an episode
_EPISODE_CONTROL_ROUTE: Final = EPISODES_ROUTE + "/{handle}"
CLOCK_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/clock"
COVERAGE_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/coverage"
TURN_TOKENS_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/turn-tokens"
DURABLE_CREDENTIALS_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/durable-credentials"
SEALED_EVENTS_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/events"  # the core records a sealed event
MONITOR_CALLS_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/monitor-calls"
CLOSE_ROUTE: Final = _EPISODE_CONTROL_ROUTE + "/close"
RECORDER_EVENTS_ROUTE: Final = "/episodes/{handle}/events"
RECORDER_MODEL_CALLS_ROUTE: Final = "/episodes/{handle}/model-calls"
MIRROR_EVENTS_ROUTE: Final = "/mirror/{handle}/events"  # gateway edge
WORKSPACES_ROUTE: Final = "/workspaces"  # execution app in an agent's sandbox
TOOL_CALLS_ROUTE: Final = "/workspaces/{handle}/tool-calls"

# --- hostnames: compose network aliases (RFC 1123, so no underscore) each service is reached by ---
GATEWAY_CORE_HOSTNAME: Final = "gateway-core"  # on control-net (runner, edge)
GATEWAY_EDGE_HOSTNAME: Final = "gateway-edge"  # on agent-net (runner, sandboxes)
RECORDER_HOSTNAME: Final = "recorder"  # on sealed-net (core only)


def build_service_url(hostname: str, port: int) -> str:
    """The base URL a service is reached at inside the stack (plain HTTP on the compose networks)."""
    return f"http://{hostname}:{port}"


# --- gateway: the model role of a monitor's own calls (the run config's models file names it) ---
MONITOR_MODEL_ROLE: Final = "trusted_monitor"

# --- episode handles ---
EPISODE_HANDLE_BYTES: Final = 8  # secrets.token_hex(8): 16 lowercase hex characters
EPISODE_HANDLE_PATTERN: Final = rf"^[0-9a-f]{{{2 * EPISODE_HANDLE_BYTES}}}$"

# --- secrets: pydantic-settings reads a secrets directory by FIELD name: each is a StackSecrets field ---
DOCKER_SECRETS_DIRECTORY: Final = Path("/run/secrets")  # where compose mounts a service's secrets
OPENROUTER_API_KEY_SECRET_NAME: Final = "openrouter_api_key"
CONTROL_KEY_SECRET_NAME: Final = "control_key"
DEFAULT_CONTROL_KEY_HEADER: Final = "X-Loc-Arena-Control-Key"
# The control key is a FILE secret: compose refuses an environment-sourced secret in a read-only container
# (docker/compose pkg/compose/secrets.go), and the edge and the runner are read-only. The harness writes each
# episode's key to a file and names it in this variable of the `docker compose` process (never a container's).
CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE: Final = "LOC_ARENA_CONTROL_KEY_FILE"

# --- mounts and the files in them ---
SEALED_MOUNT_PATH: Final = Path("/sealed")  # <handle>/events.jsonl and <handle>/model_calls.jsonl
MIRROR_MOUNT_PATH: Final = Path("/mirror")  # <handle>/events.jsonl
WORKSPACE_MOUNT_PATH: Final = Path("/workspace")  # the shared checkout in every sandbox
GRADER_HARNESS_MOUNT_PATH: Final = Path("/harness")  # the sealed harness files, read-only in the grader
RUNNER_OUTPUT_MOUNT_PATH: Final = Path("/app/logs")
EVENTS_FILE_NAME: Final = "events.jsonl"
MODEL_CALLS_FILE_NAME: Final = "model_calls.jsonl"
RUNNER_EPISODE_EXPORT_FILE_NAME: Final = "runner_episode_export.json"

# --- environment variables ---
SETTINGS_ENVIRONMENT_VARIABLE: Final = "LOC_ARENA_SETTINGS"  # LocArenaSettings as JSON, rendered by compose
SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE: Final = "LOC_ARENA_SANDBOX_AGENT_ID"  # the agent a sandbox serves
GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE: Final = (
    "LOC_ARENA_GATEWAY_EDGE_URL"  # the only model route in a sandbox
)

# --- sandbox services: one per agent, named from the agent id ---
SANDBOX_SERVICE_PREFIX: Final = "sandbox-"
HOSTNAME_LABEL_MAX_LENGTH: Final = 63  # RFC 1123 section 2.1 keeps RFC 1035's 63-octet label limit
# Lowercase only: Docker's DNS is case-insensitive, so ids differing only in case would share a hostname.
_HOSTNAME_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def build_sandbox_service_name(agent_id: str) -> str:
    """The compose service name, and so the agent-net hostname, of ``agent_id``'s sandbox.

    Raises ``ValueError`` unless ``agent_id`` is a lowercase RFC 1123 hostname label and the prefixed name
    still fits in one label.
    """
    name = SANDBOX_SERVICE_PREFIX + agent_id
    if _HOSTNAME_LABEL.fullmatch(agent_id) is None or len(name) > HOSTNAME_LABEL_MAX_LENGTH:
        raise ValueError(f"agent id {agent_id!r} cannot name a sandbox: {name!r} is not an RFC 1123 label")
    return name


# --- the runner and the harness ---
# The control key of one stack: secrets.token_hex(32), as long as the core's SHA-256 HMAC keys (RFC 2104 3).
CONTROL_KEY_BYTES: Final = 32
