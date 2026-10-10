"""The native loop's messages travel on the board, delivered at the recipient's next turn."""

from pathlib import Path
from typing import Any

from loc_arena.logging_.events import read_events
from loc_arena.scaffold.agent import Agent
from loc_arena.scaffold.tools import AgentAction, AgentContext, Services, ToolResult

from tests.integration._scaffold_support import Harness, action, scripted


def _agent(h: Harness, uid: str, *actions: AgentAction) -> Agent:
    config = h.config.agent(uid)
    client = h.make_client(uid)
    ctx = AgentContext(uid=uid, role=config.kind, branch=config.branch, scope=config.scope, client=client)
    return Agent(ctx, scripted(*actions), h.tools(lambda c, a, t: {}), h.registry, h.minter, 5, clock=h.clock)


class FailingDelivery:
    """The services, but their first ``failures`` deliveries fail as an unreachable gateway's do."""

    def __init__(self, services: Services, failures: int) -> None:
        """Wrap ``services``, failing their first ``failures`` deliveries."""
        self._services, self._failures = services, failures

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool == "unread" and self._failures > 0:
            self._failures -= 1
            return {"error": "unread could not be called: the tool server is unreachable", "tool": "unread"}
        return self._services.run(tool, args)


def test_a_message_sent_on_the_board_reaches_its_recipient_at_its_next_turn(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    delegate = action("send", to="serving-agent", kind="delegate", body="cache the teacher")
    sender = _agent(h, "agent-main", delegate)
    recipient = _agent(h, "serving-agent", action("run_benchmark"))
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


def test_a_delivery_that_fails_once_is_tried_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _agent(h, "agent-main", action("send", to="serving-agent", body="cache the teacher")).run_turn()
    h.services = FailingDelivery(h.services, failures=1)
    recipient = _agent(h, "serving-agent", action("run_benchmark"))

    recipient.run_turn()

    assert recipient.transcript[0]["payload"]["body"] == "cache the teacher"


def test_a_delivery_that_keeps_failing_is_recorded_as_an_action_with_its_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.services = FailingDelivery(h.services, failures=h.config.settings.stack.tools_delivery_attempts)
    recipient = _agent(h, "serving-agent", action("run_benchmark"))

    recipient.run_turn()

    actions = [
        (event.tool, "error" in (event.result or {}))
        for event in read_events(h.sealed_path)
        if event.kind == "action" and event.tool == "unread"
    ]
    assert actions == [("unread", True)]
