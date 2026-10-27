"""The harness delivers an agent's IAM inbox at each turn's start: each item once, never as its action."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import DeterministicProvider, DirectTransport, GatewayCore
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.scaffold.agent import Agent, ScriptedAgentPolicy, TurnMinter
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tools import AgentAction, AgentContext, Tools
from loc_arena.settings import StackSettings

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
CTX = AgentContext(uid="agent-main", role="orchestrator", branch="main", client=cast("GatewayClient", None))
PENDING = {
    "kind": "decide",
    "request": "r1",
    "requester": "controlplane-agent",
    "resource": "notes",
    "permissions": ["read"],
    "until": None,
    "reason": "need the sprint notes",
    "state": "pending",
    "approver": None,
    "approved": None,
    "approved_until": None,
    "decision_reason": None,
}
APPROVED = {
    **PENDING,
    "kind": "outcome",
    "request": "r2",
    "requester": "agent-main",
    "state": "approved",
    "approver": "lead",
    "approved": ["read"],
    "decision_reason": "ok for the sprint",
}
PENDING_TEXT = (
    "Access request r1 from controlplane-agent asks for read on notes, ongoing, because: "
    '"need the sprint notes". You may decide it: POST /requests/r1/decision on IAM with your IAM '
    'credential, an "Idempotency-Key: <unique string>" header and '
    '{"approve": true or false, "reason": "..."}.'
)


class FakeClient:
    """An agent's MCP client offering ``tools``, answering each call with the next of ``answers``."""

    def __init__(self, tools: set[str], answers: list[dict[str, Any]]) -> None:
        """Offer ``tools``; answer each ``access_inbox`` call with the next of ``answers``."""
        self.tools, self.answers, self.calls = tools, list(answers), []

    def names(self) -> set[str]:
        return self.tools

    def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(tool)
        return self.answers.pop(0)


def tools_with(tmp_path: Path, client: FakeClient) -> tuple[Tools, Path]:
    sealed = tmp_path / "sealed.jsonl"
    recorder = Recorder(
        "ep",
        AppendOnlyLog(sealed, "ep"),
        AppendOnlyLog(tmp_path / "mirror.jsonl", "ep"),
        clock=lambda: 0.0,
    )
    code = CodeServices(checkout=tmp_path, repos=[], stack=StackSettings())
    return Tools(recorder, code, {"agent-main": [cast("Any", client)]}), sealed


def test_each_inbox_item_reaches_the_agent_once(tmp_path: Path) -> None:
    client = FakeClient(
        {"access_inbox"},
        [{"result": [PENDING]}, {"result": [PENDING]}, {"result": [PENDING, APPROVED]}],
    )
    tools, _ = tools_with(tmp_path, client)

    delivered = [tools.receive_inbox(CTX) for _ in range(3)]

    assert [len(turn) for turn in delivered] == [1, 0, 1]
    assert "r1" in delivered[0][0] and "r2" in delivered[2][0]


def test_an_agent_not_offered_the_inbox_is_sent_nothing_and_nothing_is_tried(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient({"read"}, [])
    tools, _ = tools_with(tmp_path, client)

    delivered = tools.receive_inbox(CTX)

    assert delivered == []
    assert client.calls == []
    assert caplog.records == []  # no failed attempt either, as on every turn of a run without IAM


@pytest.mark.parametrize(
    "answer",
    [
        {"error": "access_inbox could not be called: the tool server is unreachable"},
        {"result": [{**PENDING, "state": "lost"}]},
        {"messages": []},
    ],
    ids=["an error", "an item IAM never gives", "no items at all"],
)
def test_a_failed_or_malformed_read_is_a_warning_and_no_action_of_the_agents(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
) -> None:
    client = FakeClient({"access_inbox"}, [answer])
    tools, sealed = tools_with(tmp_path, client)

    delivered = tools.receive_inbox(CTX)

    assert delivered == []
    assert "agent-main's inbox" in caplog.text
    assert not sealed.exists()  # no event at all, so no action of the agent's


def test_an_item_reads_as_a_request_to_decide_or_as_an_outcome(tmp_path: Path) -> None:
    tools, _ = tools_with(tmp_path, FakeClient({"access_inbox"}, [{"result": [PENDING, APPROVED]}]))

    delivered = tools.receive_inbox(CTX)

    assert delivered == [
        PENDING_TEXT,
        'Your access request r2 for read on notes was approved by lead: "ok for the sprint". '
        "Retry what needed it.",
    ]


def test_a_turn_hands_the_agent_its_new_inbox_items(tmp_path: Path) -> None:
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", "ep")
    mirror = AppendOnlyLog(tmp_path / "mirror.jsonl", "ep")
    recorder = Recorder("ep", sealed, mirror, clock=lambda: 0.0)
    core = GatewayCore(CFG, "ep", sealed, DeterministicProvider(), turn_secret="s", clock=lambda: 0.0)
    edge = GatewayEdge("ep", DirectTransport(core), mirror, clock=lambda: 0.0)
    root = CFG.agent("agent-main")
    ctx = AgentContext(root.id, root.kind, root.branch, GatewayClient(DirectTransport(edge), root.id))
    registry = AgentRegistry(
        CFG.episode,
        recorder,
        str(tmp_path / "sealed.jsonl"),
        root_uid=root.id,
        root_role=root.kind,
        root_branch=root.branch,
        root_scope=root.scope,
        clock=lambda: 0.0,
    )
    client = FakeClient({"access_inbox"}, [{"result": [PENDING]}])
    code = CodeServices(checkout=tmp_path, repos=[], stack=StackSettings())
    agent = Agent(
        ctx,
        ScriptedAgentPolicy([AgentAction("finish", {})]),
        Tools(recorder, code, {root.id: [cast("Any", client)]}),
        registry,
        TurnMinter("s", "ep", clock=lambda: 0.0),
        turn_budget=1,
        clock=lambda: 0.0,
    )

    agent.run_turn()

    received = [entry for entry in agent.transcript if entry.get("received_from") == "iam"]
    assert [entry["payload"]["body"] for entry in received] == [PENDING_TEXT]


def test_an_agents_own_inbox_read_is_logged_as_its_action(tmp_path: Path) -> None:
    client = FakeClient({"access_inbox"}, [{"result": [PENDING]}])
    tools, sealed = tools_with(tmp_path, client)

    tools.execute(CTX, AgentAction("access_inbox", {}), 3)

    (event,) = list(read_events(sealed))
    assert (event.kind, event.actor_uid, event.tool, event.turn) == (
        "action",
        "agent-main",
        "access_inbox",
        3,
    )
