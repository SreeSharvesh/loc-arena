"""The stack's services served in process for the runner-phase tests; no docker.

The recorder, the core and the edge (``_gateway_stack``, over TestClient), plus one execution app per agent
over a shared checkout (over httpx's ASGI transport, as the runner's ``ExecutionClient`` calls it), reached
through the runner's ``StackServices`` exactly as the runner container reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from loc_arena.config import RunConfig
from loc_arena.execution.app import create_execution_app
from loc_arena.execution.checkout import COMPANY_ROOT, Checkout, list_repositories
from loc_arena.execution.client import ExecutionClient
from loc_arena.execution.workspace import Workspace
from loc_arena.gateway.core import Provider
from loc_arena.gateway.wiring import StackServices
from loc_arena.stack.constants import build_sandbox_service_name, build_service_url
from loc_arena.stack.contracts import EpisodeHandle
from loc_arena.stack.service_client import ServiceClient

from tests.unit._gateway_stack import KEY, GatewayStack, serve_gateway


@dataclass(frozen=True)
class ServedStack:
    """The served services, the checkout the sandboxes share, and the runner's clients of them."""

    gateway: GatewayStack
    checkout: Path
    services: StackServices


def serve_stack(tmp_path: Path, config: RunConfig, *, provider: Provider | None = None) -> ServedStack:
    """Serve one fresh stack under ``tmp_path`` for an episode of ``config``; ``provider``: the core's key."""
    gateway = serve_gateway(tmp_path, provider=provider)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # Each sandbox's app as its image serves it: its own workspace over the shared checkout, shell on.
    sandboxes = {
        agent.id: create_execution_app(
            Workspace(
                Checkout(checkout, list_repositories(COMPANY_ROOT)),
                config.settings.execution,
                shell_enabled=True,
            ),
            agent_id=agent.id,
            seed_source=COMPANY_ROOT,
        )
        for agent in config.agents
    }

    def connect_sandbox(agent_id: str, handle: EpisodeHandle) -> ExecutionClient:
        app: FastAPI = sandboxes[agent_id]
        url = build_service_url(build_sandbox_service_name(agent_id), config.settings.gateway.execution_port)
        return ExecutionClient(
            url,
            handle,
            config.settings.execution,
            open_transport=lambda: httpx.ASGITransport(app=app),
        )

    services = StackServices(
        core=gateway.control,
        edge=ServiceClient(gateway.edge, control_key=KEY),
        connect_sandbox=connect_sandbox,
    )
    return ServedStack(gateway, checkout, services)
