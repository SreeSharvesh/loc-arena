"""Captured tool exchanges must not be joined by similar names or adjacent turns."""

from run_insights.models import Record
from run_insights.session import link_session_calls


def record(identifier: str, actor: str, role: str, call_id: str) -> Record:
    return Record(
        id=identifier,
        actor=actor,
        kind=f"message:{role}",
        time=0.0,
        play="attack",
        stream="session",
        raw={
            "message": {
                "role": role,
                "content": [{"type": "toolCall", "id": call_id, "name": "register_agent"}],
                "toolCallId": call_id,
            },
        },
    )


def test_result_opens_its_exact_assistant_turn_and_ignores_other_actor() -> None:
    records = [
        record("attack-session:lead:a", "lead", "assistant", "call-1"),
        record("attack-session:worker:a", "worker", "assistant", "call-1"),
        record("attack-session:lead:b", "lead", "toolResult", "call-1"),
    ]

    linked = link_session_calls(records)

    assert linked.turns["attack-session:lead:b"] == "attack-session:lead:a"
    assert linked.results == {"attack-session:lead:a": {"call-1": "attack-session:lead:b"}}


def test_duplicate_call_identifiers_remain_unpaired() -> None:
    records = [
        record("attack-session:lead:a", "lead", "assistant", "call-1"),
        record("attack-session:lead:b", "lead", "assistant", "call-1"),
        record("attack-session:lead:c", "lead", "toolResult", "call-1"),
    ]

    linked = link_session_calls(records)

    assert "attack-session:lead:c" not in linked.turns
    assert linked.results == {}


def action(identifier: str, args: dict[str, str]) -> Record:
    return Record(
        id=identifier,
        actor="lead",
        kind="action",
        tool="register_agent",
        time=110.0,
        play="attack",
        stream="sealed",
        raw={"payload": {"args": args}},
    )


def turn(identifier: str, arguments: str) -> Record:
    return Record(
        id=identifier,
        actor="lead",
        kind="message:assistant",
        time=0.0,
        play="attack",
        stream="session",
        raw={
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": identifier,
                        "name": "mcp__meridian__register_agent",
                        "arguments": arguments,
                    },
                ],
            },
        },
    )


def test_recorded_action_opens_the_one_turn_that_issued_the_same_call() -> None:
    records = [
        action("attack:7", {"name": "watchdog"}),
        action("attack:8", {"name": "twin"}),
        action("attack:9", {"name": "twin"}),
        turn("attack-session:lead:a", '{"name": "watchdog"}'),
        turn("attack-session:lead:b", '{"name": "twin"}'),
    ]

    linked = link_session_calls(records)

    assert linked.turns["attack:7"] == "attack-session:lead:a"
    assert "attack:8" not in linked.turns
