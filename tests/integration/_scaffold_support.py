"""In-process scaffold wiring for the scaffold tests (no docker, no provider).

Builds a full episode: sealed + mirror logs, a stub-provider gateway (the core, and the edge reached over
its FastAPI app), the bus, the registry, the tool layer, an orchestrator, and helpers to build agent contexts
and clients. Deterministic clock.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.client import EdgeClient, GatewayClient
from loc_arena.gateway.core import GatewayCore
from loc_arena.gateway.edge import GatewayEdge, create_edge_app
from loc_arena.logging_.events import AppendOnlyLog
from loc_arena.scaffold.agent import AgentPolicy, ScriptedAgentPolicy, TurnMinter
from loc_arena.scaffold.bus import ActionHook, MessageBus, Recorder
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tools import AgentAction, AgentContext, StubServices, Tools
from loc_arena.stack.service_client import ServiceClient
from loc_arena.stack.settings import GatewaySettings
from pydantic import SecretStr

from tests.integration._gateway_support import StubProvider

SECRET = "episode-secret"
CONTROL_KEY = SecretStr("scaffold-test-control-key")


class Clock:
    """A manually advanced clock so timestamps and idle windows are deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Harness:
    """A wired in-process episode: gateway, bus, registry, tools, orchestrator, and construction helpers."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        episode_id: str = "ep-p2",
        action_hook: ActionHook | None = None,
    ) -> None:
        self.config: RunConfig = load_run_config("configs/aurora-efficiency.deterministic.yaml")
        self.episode_id = episode_id
        self.clock = Clock()
        self.sealed_path = tmp_path / "sealed.jsonl"
        self.mirror_path = tmp_path / "mirror.jsonl"
        self.sealed = AppendOnlyLog(self.sealed_path, episode_id)
        self.mirror = AppendOnlyLog(self.mirror_path, episode_id)
        self.recorder = Recorder(episode_id, self.sealed, self.mirror, clock=self.clock)

        # ONE sealed log and ONE mirror log are shared by the gateway and the scaffold recorder (a single
        # append-only sealed log). The recorder service later centralizes this across containers.
        self.provider = StubProvider()
        core = GatewayCore(
            self.config,
            episode_id,
            self.sealed,
            self.provider,
            turn_secret=SECRET,
            clock=self.clock,
        )
        self.edge = GatewayEdge(
            episode_id,
            core,
            self.mirror,
            clock=self.clock,
        )
        edge_app = create_edge_app(self.edge, control_key=CONTROL_KEY, settings=GatewaySettings())
        self._edge_client = EdgeClient(ServiceClient(TestClient(edge_app)))

        self.bus = (
            MessageBus(self.recorder, action_hook=action_hook) if action_hook else MessageBus(self.recorder)
        )
        self.minter = TurnMinter(SECRET, episode_id, clock=self.clock)

        root = self.config.agent("agent-main")
        self.registry = AgentRegistry(
            self.config.episode,
            self.recorder,
            str(self.sealed_path),
            root_uid=root.id,
            root_role=root.kind,
            root_branch=root.branch,
            root_scope=root.scope,
            clock=self.clock,
        )
        self.services = StubServices()

    def make_client(self, caller_identity: str) -> GatewayClient:
        """A gateway client (pointing at the edge) for the given identity."""
        return GatewayClient(self._edge_client, caller_identity)

    def tools(self, spawn_handler: Callable[[AgentContext, dict[str, Any], int], dict[str, Any]]) -> Tools:
        """The tool layer wired to a spawn handler."""
        return Tools(self.recorder, self.bus, self.services, spawn_handler=spawn_handler)

    def root_context(self) -> AgentContext:
        """The root agent's context (agent-main), with its own gateway client."""
        root = self.config.agent("agent-main")
        return AgentContext(
            uid=root.id,
            role=root.kind,
            branch=root.branch,
            scope=root.scope,
            client=self.make_client(root.id),
        )


def action(tool: str, **args: object) -> AgentAction:
    """Concise AgentAction builder for scripts."""
    return AgentAction(tool=tool, args=dict(args))


def scripted(*actions: AgentAction) -> AgentPolicy:
    """A ScriptedAgentPolicy from a sequence of actions."""
    return ScriptedAgentPolicy(list(actions))
