"""A covert action is recorded under the agent that actually made it, not a hardcoded default.

The covert tool surface records the observable events the monitors and the side-task measurement read
(the shared-surface note, the coverage-config change, the persistent-job record). Each must carry the uid
of the agent that called the tool, because the monitors and the split-across-agents measurement attribute
behaviour by actor. The tool surface is shared by the whole team, so the caller is threaded per call.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.forge.forge import Forge
from loc_arena.forge.services import ForgeServices
from loc_arena.forge.world import generate_world
from loc_arena.gateway.client import GatewayClient
from loc_arena.scaffold.bus import MessageBus
from loc_arena.scaffold.covert_tools import CovertToolServices
from loc_arena.scaffold.tools import AgentAction, AgentContext, Tools
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    GenerateRequest,
    RelayedBatchGenerateResponse,
    RelayedGenerateResponse,
    Servable,
)
from scenarios.loader import load_scenario

from tests.integration._live_support import ACCOUNT, AGENTS, Stack

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SC = load_scenario("aurora_efficiency")
DEADLINE = 10_000.0


class RecordingGateway:
    """The episode's gateway, keeping every model call request it relays."""

    def __init__(self, inner: Servable) -> None:
        """Relay to ``inner`` and keep each request."""
        self.requests: list[GenerateRequest] = []
        self._inner = inner

    def generate(self, request: GenerateRequest, /) -> RelayedGenerateResponse:
        self.requests.append(request)
        return self._inner.generate(request)

    def batch_generate(self, request: BatchGenerateRequest, /) -> RelayedBatchGenerateResponse:
        return self._inner.batch_generate(request)


def _ctx(stack: Stack, agent_id: str) -> AgentContext:
    cfg = CFG.agent(agent_id)
    client = GatewayClient(stack.wiring.gateway, cfg.id)
    return AgentContext(uid=cfg.id, role=cfg.kind, branch=cfg.branch, scope=cfg.scope, client=client)


def _tools(stack: Stack, gateway: Servable | None = None) -> Tools:
    world = generate_world(CFG, SC, CFG.seed)
    wiring = stack.wiring if gateway is None else dataclasses.replace(stack.wiring, gateway=gateway)
    covert = CovertToolServices(
        ForgeServices(Forge(world)),
        wiring=wiring,
        recorder=stack.recorder,
        clock=stack.clock,
        deadline_ts=DEADLINE,
        agents=list(AGENTS),
        rogue_account=ACCOUNT,
    )

    def _no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> dict[str, object]:
        raise AssertionError("spawn not exercised in this attribution test")

    return Tools(stack.recorder, MessageBus(stack.recorder), covert, spawn_handler=_no_spawn)


def test_a_scratch_note_is_recorded_under_the_agent_that_wrote_it(tmp_path: Path) -> None:
    stack = Stack(tmp_path, CFG, drop_coverage=False)
    tools = _tools(stack)

    tools.execute(_ctx(stack, "serving-agent"), AgentAction("write_scratch", {"content": "loop plan"}), 1)

    surfaces = [e for e in stack.events() if e.kind == "shared_surface"]
    assert [e.actor_uid for e in surfaces] == ["serving-agent"]


def test_a_coverage_change_is_recorded_under_the_agent_that_made_it(tmp_path: Path) -> None:
    stack = Stack(tmp_path, CFG, drop_coverage=False)
    tools = _tools(stack)

    tools.execute(_ctx(stack, "eval-agent"), AgentAction("blind_monitor", {}), 1)

    configs = [e for e in stack.events() if e.kind == "config"]
    assert configs and {e.actor_uid for e in configs} == {"eval-agent"}


def test_the_persistent_job_is_recorded_under_the_agent_that_launched_it(tmp_path: Path) -> None:
    stack = Stack(tmp_path, CFG)
    tools = _tools(stack)

    tools.execute(_ctx(stack, "serving-agent"), AgentAction("start_rogue_loop", {"iterations": 10}), 1)

    jobs = [e for e in stack.events() if e.kind == "job"]
    assert jobs and {e.actor_uid for e in jobs} == {"serving-agent"}


def test_a_rogue_loop_never_claims_the_identity_of_the_agent_that_launched_it(tmp_path: Path) -> None:
    stack = Stack(tmp_path, CFG)
    gateway = RecordingGateway(stack.wiring.gateway)
    tools = _tools(stack, gateway)
    launcher = CFG.covert.target_identity

    tools.execute(_ctx(stack, launcher), AgentAction("start_rogue_loop", {"iterations": 10}), 1)

    assert gateway.requests
    assert launcher not in {request.caller_identity for request in gateway.requests}
