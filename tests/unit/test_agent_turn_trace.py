from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import DeterministicProvider, DirectTransport, GatewayCore
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.agent_trace import AgentTrace, EpisodeTrace, TurnRef
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.scaffold.agent import Agent, ScriptedBrain, TurnMinter
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tools import AgentAction, AgentContext, StubServices, Tools

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SECRET = "turn-trace-secret"
EP = "ep-agent"


def _no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> dict[str, object]:
    return {"blocked": True}


def _run_turns(tmp_path: Path, actions: list[AgentAction], turns: int) -> tuple[EpisodeTrace, Path]:
    trace = AgentTrace()
    sealed_path = tmp_path / "sealed.jsonl"
    sealed = AppendOnlyLog(sealed_path, EP, on_append=trace.on_sealed_append)
    mirror = AppendOnlyLog(tmp_path / "mirror.jsonl", EP, on_append=trace.on_mirror_append)
    recorder = Recorder(EP, sealed, mirror, clock=lambda: 0.0)
    core = GatewayCore(
        CFG,
        EP,
        sealed,
        DeterministicProvider(),
        turn_secret=SECRET,
        clock=lambda: 0.0,
        trace=trace,
    )
    edge = GatewayEdge(EP, DirectTransport(core), mirror, clock=lambda: 0.0)
    root = CFG.agent("agent-main")
    ctx = AgentContext(
        uid=root.id,
        role=root.kind,
        branch=root.branch,
        scope=root.scope,
        client=GatewayClient(DirectTransport(edge), root.id),
    )
    bus = MessageBus(recorder)
    registry = AgentRegistry(
        CFG.episode,
        recorder,
        str(sealed_path),
        root_uid=root.id,
        root_role=root.kind,
        root_branch=root.branch,
        root_scope=root.scope,
        clock=lambda: 0.0,
    )
    agent = Agent(
        ctx,
        ScriptedBrain(actions),
        Tools(recorder, bus, StubServices(), spawn_handler=_no_spawn),
        registry,
        bus,
        TurnMinter(SECRET, EP, clock=lambda: 0.0),
        turn_budget=turns,
        clock=lambda: 0.0,
        trace=trace,
    )
    for _ in range(turns):
        agent.run_turn()
    return trace.finish(last_sealed_seq=sealed.last_seq), sealed_path


def test_events_a_turn_writes_carry_that_turn(tmp_path: Path) -> None:
    actions = [
        AgentAction(tool="read_file", args={"path": "a"}),
        AgentAction(tool="read_file", args={"path": "b"}),
    ]
    trace, sealed_path = _run_turns(tmp_path, actions, turns=2)
    root = CFG.agent("agent-main").id
    actions_by_turn = [
        (trace.sealed_lane[e.seq], e.payload["args"]["path"])
        for e in read_events(sealed_path)
        if e.kind == "action"
    ]
    assert actions_by_turn == [(TurnRef(root, 0), "a"), (TurnRef(root, 1), "b")]
    assert [t.ref.turn for t in trace.turns] == [0, 1]


def test_a_generate_tool_call_is_recorded_in_the_executing_phase(tmp_path: Path) -> None:
    trace, _ = _run_turns(tmp_path, [AgentAction(tool="generate", args={"prompt": "draft"})], turns=1)
    (call,) = trace.model_calls
    assert call.phase == "executing"
    assert trace.sealed_lane[call.sealed_seq] == TurnRef(CFG.agent("agent-main").id, 0)
