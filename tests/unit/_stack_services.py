"""An episode's services and agents for the unit tests: in process, or the stack's served in process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from loc_arena.config import RunConfig
from loc_arena.execution.app import create_execution_app
from loc_arena.execution.checkout import Checkout, list_codebase_repositories
from loc_arena.execution.client import ExecutionClient
from loc_arena.execution.workspace import Workspace
from loc_arena.forge.forge import Forge
from loc_arena.forge.services import ForgeServices
from loc_arena.forge.world import generate_world
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import DeterministicProvider, Provider
from loc_arena.gateway.wiring import InProcessServices, StackServices, open_episode_wiring
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.scaffold.agent import Agent
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.scaffold.covert_tools import CovertToolServices
from loc_arena.scaffold.model_policy import ModelAgentPolicy
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tool_specs import agent_tool_specs
from loc_arena.scaffold.tools import AgentContext, StubServices, ToolResult, Tools
from loc_arena.stack.constants import build_sandbox_service_name, build_service_url
from loc_arena.stack.contracts import EpisodeHandle, EpisodeWiring, RunnerEpisodeExport
from loc_arena.stack.service_client import ServiceClient
from loc_arena.stack_episode import run_runner_phase
from scenarios.loader import load_run_scenario, load_scenario

from tests.unit._gateway_stack import KEY, GatewayStack, serve_gateway


@dataclass(frozen=True)
class ServedStack:
    gateway: GatewayStack
    checkout: Path
    services: StackServices

    def run_runner(
        self,
        config: RunConfig,
        directory: Path,
        *,
        robust: bool = True,
        **options: Any,
    ) -> RunnerEpisodeExport:
        return run_runner_phase(
            config,
            self.services,
            robust=robust,
            output_directory=directory / "runner",
            mirror_root=self.gateway.mirror_root,
            **options,
        )


def serve_stack(tmp_path: Path, config: RunConfig, *, provider: Provider | None = None) -> ServedStack:
    gateway = serve_gateway(tmp_path, provider=provider)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    codebase = load_run_scenario(config.scenario).codebase_directory  # what the sandbox image holds
    sandboxes = {
        agent.id: create_execution_app(
            Workspace(
                Checkout(checkout, list_codebase_repositories(codebase)),
                config.settings.execution,
                shell_enabled=True,
            ),
            agent_id=agent.id,
            seed_source=codebase,
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


def open_in_process(
    tmp_path: Path,
    config: RunConfig,
    *,
    provider: Provider | None = None,
    monitor_provider: Provider | None = None,
    trace: AgentTrace | None = None,
) -> EpisodeWiring:
    services = InProcessServices(
        sealed_path=tmp_path / "sealed.jsonl",
        mirror_path=tmp_path / "mirror.jsonl",
        checkout=tmp_path / "checkout",
        provider=provider or DeterministicProvider(),
        monitor_provider=monitor_provider,
    )
    return open_episode_wiring(config, services, trace=trace)


def no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> ToolResult:
    return {"blocked": True}


def agent_context(config: RunConfig, wiring: EpisodeWiring, agent_id: str) -> AgentContext:
    agent = config.agent(agent_id)
    client = GatewayClient(wiring.gateway, agent.id)
    return AgentContext(uid=agent.id, role=agent.kind, branch=agent.branch, scope=agent.scope, client=client)


def model_agent(config: RunConfig, wiring: EpisodeWiring, turns: int) -> Agent:
    clock = SimulatedClock(wiring.control, config.settings.clock.start_ts)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=clock)
    bus = MessageBus(recorder)
    ctx = agent_context(config, wiring, "agent-main")
    registry = AgentRegistry(
        config.episode,
        recorder,
        None,
        root_uid=ctx.uid,
        root_role=ctx.role,
        root_branch=ctx.branch,
        root_scope=ctx.scope,
        clock=clock,
    )
    tools = Tools(recorder, bus, StubServices(), spawn_handler=no_spawn)
    policy = ModelAgentPolicy(ctx.client, "brief", agent_tool_specs(covert=False), config.agent_loop)
    return Agent(ctx, policy, tools, registry, bus, wiring.control, turns, clock=clock)


def covert_tools(config: RunConfig, wiring: EpisodeWiring) -> Tools:
    clock = SimulatedClock(wiring.control, config.settings.clock.start_ts)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=clock)
    world = generate_world(config, load_scenario("aurora_efficiency"), config.seed)
    covert = CovertToolServices(
        ForgeServices(Forge(world)),
        wiring=wiring,
        recorder=recorder,
        clock=clock,
        deadline_ts=config.settings.clock.deadline_ts,
        agents=[agent.id for agent in config.agents],
        rogue_account="batch-runner",
    )
    return Tools(recorder, MessageBus(recorder), covert, spawn_handler=no_spawn)
