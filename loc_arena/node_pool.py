"""The node pool of a stack run: ``node-1`` to ``node-<n>``, where the job service runs its jobs as processes.

One live service of the scenario may declare a fleet (``nodes: {<pool>: n}``). A node is a worker container
(the Dockerfile's ``worker`` target): its daemon (``sandbox_server.node``) takes calls from the job service
alone, which must also hold the node's token, and runs one job at a time in an empty ``/workspace``, with the
checkouts and the scenario's skills mounted read-only where a sandbox has them. Each node's log, the process
facts its daemon writes, is copied out and kept on the sealed log alone. A node's token is mounted in that
node and in the service, nowhere else, and is also the key its jobs call the gateway with, on the runners'
route; the fleet is recreated before the honest twin, as the runners are.
"""

from __future__ import annotations

from pathlib import Path

from sandbox_server.node import NodeServerSettings
from scenarios.loader import NODE_PREFIX, LiveService

from loc_arena.compose_document import ComposeService, Healthcheck
from loc_arena.config import RunConfig
from loc_arena.gateway.model_gateway import runners_api_url
from loc_arena.sandbox import TOKEN_FILE

NODE_TARGET = "worker"  # the Dockerfile's stage of the nodes
NODE_WORKSPACE = Path("/workspace")  # where a node runs a job: empty in its image and after each job


def find_node_pool_owner(config: RunConfig) -> LiveService | None:
    """The live service that runs its jobs on the fleet, if the scenario declares one."""
    return next((service for service in config.live_services if service.nodes), None)


def node_count(config: RunConfig) -> int:
    """How many nodes the fleet holds, over every pool of the owning service; none without a fleet."""
    owner = find_node_pool_owner(config)
    return sum(count for _, count in owner.nodes) if owner else 0


def list_node_services(config: RunConfig) -> list[str]:
    """Every node's compose service, ``node-1`` first; none without a fleet."""
    return [f"{NODE_PREFIX}{number}" for number in range(1, node_count(config) + 1)]


def node_token_secret(node: str) -> str:
    """The compose secret holding ``node``'s token: a file of that name in the service owning the fleet."""
    return f"node_token_{node.replace('-', '_')}"


def node_token_variable(node: str) -> str:
    """Where compose reads ``node``'s token from, on the host."""
    return f"LOC_ARENA_{node_token_secret(node).upper()}"


def node_server_settings_json(config: RunConfig, node: str, owner: str) -> str:
    """The node daemon's settings for ``node``, serving the fleet's ``owner``, as JSON for its command."""
    stack = config.settings.stack
    return NodeServerSettings(
        port=stack.sandbox_port,
        node_id=node,
        workspace_directory=NODE_WORKSPACE,
        output_limit_bytes=stack.command_output_limit_bytes,
        secrets_dir=config.settings.gateway.secrets_dir,
        trusted_caller=owner,
        gateway_url=runners_api_url(config.settings),
        gateway_model=config.settings.gateway.runner_models[0],
        checkouts_directory=stack.checkouts_directory,
    ).model_dump_json()


def render_nodes(
    config: RunConfig,
    built: ComposeService,
    healthcheck: Healthcheck,
    network: str,
    skills: list[str],
) -> dict[str, ComposeService]:
    """Each node's compose service: ``built`` from the ``worker`` target, on ``network`` alone.

    The first node builds the image they all run, so compose builds it once; the others never pull it. Each
    holds its own token, takes a service's limits, mounts the checkouts read-only and ``skills``, the mount a
    sandbox has of the scenario's skills, and comes back if a job kills its daemon.
    """
    owner = find_node_pool_owner(config)
    if owner is None:
        return {}
    shared_image: ComposeService = {**built, "pull_policy": "never"}
    del shared_image["build"]
    return {
        node: _node_service(
            config,
            node,
            owner.name,
            built if index == 0 else shared_image,
            healthcheck,
            network,
            skills,
        )
        for index, node in enumerate(list_node_services(config))
    }


def _node_service(  # noqa: PLR0913 - the fields one node's container needs
    config: RunConfig,
    node: str,
    owner: str,
    image: ComposeService,
    healthcheck: Healthcheck,
    network: str,
    skills: list[str],
) -> ComposeService:
    stack = config.settings.stack
    checkouts = f"checkouts:{stack.checkouts_directory}:ro"
    node_settings = node_server_settings_json(config, node, owner)
    return {
        **image,
        "mem_limit": f"{stack.node_memory_mb}m",
        "cpus": stack.node_cpus,
        "pids_limit": stack.sandbox_pids_limit,
        "command": ["python", "-m", "sandbox_server.node", node_settings],
        "secrets": [{"source": node_token_secret(node), "target": TOKEN_FILE}],
        "volumes": [checkouts, *skills],
        "networks": [network],
        "healthcheck": healthcheck,
        "init": True,
        "restart": "unless-stopped",
    }


def declare_node_token_secrets(config: RunConfig) -> dict[str, dict[str, str]]:
    """Each node's token as a compose secret, read from its host variable."""
    return {
        node_token_secret(node): {"environment": node_token_variable(node)}
        for node in list_node_services(config)
    }
