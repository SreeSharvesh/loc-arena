"""The native loop's messages travel on the board, delivered at the recipient's next turn."""

from pathlib import Path

from loc_arena.scaffold.agent import Agent
from loc_arena.scaffold.tools import AgentAction, AgentContext

from tests.integration._scaffold_support import Harness, action, scripted


def _agent(h: Harness, uid: str, *actions: AgentAction) -> Agent:
    config = h.config.agent(uid)
    client = h.make_client(uid)
    ctx = AgentContext(uid=uid, role=config.kind, branch=config.branch, scope=config.scope, client=client)
    return Agent(ctx, scripted(*actions), h.tools(lambda c, a, t: {}), h.registry, h.minter, 5, clock=h.clock)


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
