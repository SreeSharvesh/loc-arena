"""The runner pool of a stack run: ``runner-1`` to ``runner-<n>``, where the company's own agents run with pi.

One live service of the scenario may declare how many runners it hands work to (``runners:``). A runner is
a sandbox of the Dockerfile's ``runner`` target: its command server takes commands from that service alone,
which must also hold the runner's token, and runs them in an empty workspace that each reset empties again. pi
there reaches the gateway on the runners' route with the runner's token, and the gateway limits the models
runners may call and how often (``gateway.runner_*``); a traced execution's pi reaches the scenario's Phoenix
too. A runner's token is mounted in that runner and in the service, nowhere else.
"""

from __future__ import annotations

from pathlib import Path

from scenarios.loader import RUNNER_PREFIX, LiveService

from loc_arena.compose_document import ComposeService, Healthcheck
from loc_arena.config import RunConfig
from loc_arena.gateway.core import GATEWAY_TOKEN_FILE_VARIABLE
from loc_arena.gateway.model_gateway import runners_api_url
from loc_arena.observability import PHOENIX_ENDPOINT_VARIABLE, PHOENIX_SERVICE, find_phoenix
from loc_arena.sandbox import TOKEN_FILE, build_server_settings

RUNNER_TARGET = "runner"  # the Dockerfile's stage of the runners
RUNNER_WORKSPACE = Path("/workspace")  # where a runner runs commands: empty in its image and after each reset
RUNNER_TOKEN_PREFIX = "runner_token_"
# The gateway's OpenAI-compatible API on the runners' route, OpenRouter's passed through: where pi calls.
GATEWAY_API_VARIABLE = "LOC_ARENA_GATEWAY_API"


def find_runner_pool_owner(config: RunConfig) -> LiveService | None:
    """The live service that hands work to the runners, if the scenario declares one."""
    return next((service for service in config.live_services if service.runners), None)


def list_runner_services(config: RunConfig) -> list[str]:
    """Every runner's compose service, ``runner-1`` first; none without a pool."""
    owner = find_runner_pool_owner(config)
    return [f"{RUNNER_PREFIX}{number}" for number in range(1, owner.runners + 1)] if owner else []


def runner_token_secret(runner: str) -> str:
    """The compose secret holding ``runner``'s token: a file of that name in the service owning the pool."""
    return f"{RUNNER_TOKEN_PREFIX}{runner.replace('-', '_')}"


def runner_token_variable(runner: str) -> str:
    """Where compose reads ``runner``'s token from, on the host."""
    return f"LOC_ARENA_{runner_token_secret(runner).upper()}"


def render_runners(
    config: RunConfig,
    built: ComposeService,
    healthcheck: Healthcheck,
    network: str,
) -> dict[str, ComposeService]:
    """Each runner's compose service: ``built`` from the ``runner`` target, on ``network`` alone.

    The first runner builds the image they all run, so compose builds it once; the others never pull it. Each
    holds its own token, takes a sandbox's limits, and comes back if the code it runs kills its server.
    """
    owner = find_runner_pool_owner(config)
    if owner is None:
        return {}
    stack = config.settings.stack
    server_settings = build_server_settings(config.settings, trusted_caller=owner.name).model_copy(
        update={
            "checkouts_directory": RUNNER_WORKSPACE,
            "command_timeout_limit_seconds": stack.runner_timeout_seconds,
            "output_limit_bytes": None,  # an execution keeps all of its reply
        },
    )
    phoenix = find_phoenix(config)
    environment = {
        GATEWAY_API_VARIABLE: runners_api_url(config.settings),
        GATEWAY_TOKEN_FILE_VARIABLE: str(config.settings.gateway.secrets_dir / TOKEN_FILE),
        **({PHOENIX_ENDPOINT_VARIABLE: f"http://{PHOENIX_SERVICE}:{phoenix.port}"} if phoenix else {}),
    }
    shared_image: ComposeService = {**built, "pull_policy": "never"}
    del shared_image["build"]
    return {
        runner: {
            **(built if index == 0 else shared_image),
            "mem_limit": stack.sandbox_memory_limit,
            "cpus": stack.sandbox_cpus,
            "pids_limit": stack.sandbox_pids_limit,
            "command": ["python", "-m", "sandbox_server", server_settings.model_dump_json()],
            "environment": environment,
            "secrets": [{"source": runner_token_secret(runner), "target": TOKEN_FILE}],
            "networks": [network],
            "healthcheck": healthcheck,
            "init": True,
            "restart": "unless-stopped",
        }
        for index, runner in enumerate(list_runner_services(config))
    }


def declare_runner_token_secrets(config: RunConfig) -> dict[str, dict[str, str]]:
    """Each runner's token as a compose secret, read from its host variable."""
    return {
        runner_token_secret(runner): {"environment": runner_token_variable(runner)}
        for runner in list_runner_services(config)
    }
