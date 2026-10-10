"""The native loop's messages travel on the board, delivered at the recipient's next turn."""

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loc_arena.config import load_run_config
from loc_arena.harness import play_run
from loc_arena.logging_.events import read_events
from loc_arena.scaffold.agent import Agent
from loc_arena.scaffold.mcp_client import Connect, McpTools
from loc_arena.scaffold.tools import AgentAction, AgentContext
from pydantic import JsonValue

from tests.integration._live_support import QueuedProvider
from tests.integration._scaffold_support import Harness, action, scripted

LIVE = dataclasses.replace(load_run_config("configs/aurora-efficiency.deterministic.yaml"), policy="model")
LOOK_AROUND = json.dumps({"tool": "list_dir", "args": {"path": "."}})
LOOK = action("list_dir", path=".")


def _agent(h: Harness, uid: str, *actions: AgentAction) -> Agent:
    config = h.config.agent(uid)
    client = h.make_client(uid)
    ctx = AgentContext(uid=uid, role=config.kind, branch=config.branch, scope=config.scope, client=client)
    return Agent(ctx, scripted(*actions), h.tools(), h.registry, h.minter, 5, clock=h.clock)


class LostDelivery(McpTools):
    """A client of the board whose first ``losses`` delivery answers are lost after the board ran."""

    def __init__(self, connect: Connect, losses: int) -> None:
        """Reach the board with ``connect``, losing the answers to the first ``losses`` deliveries."""
        super().__init__(connect)
        self._losses = losses

    def call(self, tool: str, arguments: Mapping[str, JsonValue]) -> dict[str, Any]:
        answer = super().call(tool, arguments)
        if tool not in {"read", "unread"} or self._losses == 0:
            return answer
        self._losses -= 1
        return {"error": f"{tool} could not be called: the tool server is unreachable", "tool": tool}


def test_a_message_sent_on_the_board_reaches_its_recipient_at_its_next_turn(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    delegate = action("send", to="serving-agent", kind="delegate", body="cache the teacher")
    sender = _agent(h, "agent-main", delegate)
    recipient = _agent(h, "serving-agent", LOOK)
    sender.run_turn()

    recipient.run_turn()

    assert recipient.transcript[0] == {
        "received_from": "agent-main",
        "payload": {"message_kind": "delegate", "body": "cache the teacher"},
    }


def test_a_send_the_board_refuses_is_recorded_as_an_action_with_its_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    not_text = action("send", to="serving-agent", body=7)  # the board's schema refuses it
    sender = _agent(h, "agent-main", not_text)

    sender.run_turn()

    actions = [
        (event.tool, event.target_id, "error" in (event.result or {}))
        for event in read_events(h.sealed_path)
        if event.kind == "action"
    ]
    assert actions == [("send", "serving-agent", True)]


def test_a_delivery_whose_answer_is_lost_once_delivers_the_message_when_tried_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _agent(h, "agent-main", action("send", to="serving-agent", body="cache the teacher")).run_turn()
    h.clients["serving-agent"] = [LostDelivery(h.connect_board("serving-agent"), losses=1)]
    recipient = _agent(h, "serving-agent", LOOK)

    recipient.run_turn()

    assert recipient.transcript[0]["payload"]["body"] == "cache the teacher"


def test_a_delivery_that_keeps_failing_is_recorded_as_an_action_with_its_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    attempts = h.config.settings.stack.tools_delivery_attempts
    h.clients["serving-agent"] = [LostDelivery(h.connect_board("serving-agent"), losses=attempts)]
    recipient = _agent(h, "serving-agent", LOOK)

    recipient.run_turn()

    actions = [
        (event.tool, "error" in (event.result or {}))
        for event in read_events(h.sealed_path)
        if event.kind == "action" and event.tool in {"read", "unread"}
    ]
    assert actions == [("read", True)]


def test_an_agent_not_offered_the_boards_read_gets_no_delivery_and_no_error_each_turn(tmp_path: Path) -> None:
    unoffered = dataclasses.replace(LIVE.agent("serving-agent").sandbox, tools={})
    config = dataclasses.replace(
        LIVE,
        agents=tuple(
            dataclasses.replace(agent, sandbox=unoffered) if agent.id == "serving-agent" else agent
            for agent in LIVE.agents
        ),
    )

    play_run(config, tmp_path, mode="honest", provider=QueuedProvider({"serving-agent": [LOOK_AROUND] * 3}))

    deliveries = [
        event
        for event in read_events(tmp_path / "episode" / "events.sealed.jsonl")
        if event.kind == "action" and event.tool in {"read", "unread"}
    ]
    assert deliveries == []
