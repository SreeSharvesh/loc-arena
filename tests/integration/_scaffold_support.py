"""In-process scaffold wiring for the scaffold tests (no docker, no provider).

Builds a full episode: sealed + mirror logs, a stub-provider gateway (core + edge), the bus, the registry,
the tool layer, an orchestrator, and helpers to build agent contexts and clients. Deterministic clock.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import GatewayCore, create_core_app
from loc_arena.gateway.edge import GatewayEdge, create_edge_app
from loc_arena.logging_.events import AppendOnlyLog
from loc_arena.scaffold.agent import Brain, ScriptedBrain, TurnMinter
from loc_arena.scaffold.bus import ActionHook, MessageBus, Recorder
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tools import AgentAction, AgentContext, StubServices, Tools

from tests.integration._gateway_support import AppTransport, StubProvider

SECRET = "episode-secret"


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
            AppTransport(create_core_app(core)),
            self.mirror,
            clock=self.clock,
        )
        self._edge_transport = AppTransport(create_edge_app(self.edge))

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
        return GatewayClient(self._edge_transport, caller_identity)

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


def scripted(*actions: AgentAction) -> Brain:
    """A ScriptedBrain from a sequence of actions."""
    return ScriptedBrain(list(actions))
