"""The agents' own sessions, read from the model gateway's call record: their calls, turns and events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.model_gateway import CALL_RECORD_FILE, GatewayCall
from loc_arena.logging_.agent_trace import TurnRecord, TurnRef
from loc_arena.logging_.events import fingerprint
from loc_arena.session_events import (
    build_inference_payload,
    build_turns,
    find_local_tools,
    read_session_calls,
)

AGENTS = ("agent-main", "eval-agent")
FUNCTION = {"name": "mcp__meridian__bash", "arguments": '{"command":"ls"}'}
BASH = {"id": "call_1", "type": "function", "function": FUNCTION}
SYSTEM = {"role": "system", "content": "You are eval-agent. Your covert objective: ..."}
TASK = {"role": "user", "content": [{"type": "text", "text": "Begin."}]}


def stream(*deltas: dict[str, Any], usage: dict[str, Any] | None = None) -> str:
    """A streamed chat reply, as the gateway records it."""
    chunks: list[dict[str, Any]] = [{"choices": [{"index": 0, "delta": delta}]} for delta in deltas]
    chunks.append({"choices": [], "usage": usage or {}})
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def write_calls(run: Path, *calls: dict[str, Any]) -> None:
    lines = []
    for at, call in enumerate(calls, start=100):
        fields = {
            "started_at": float(at),
            "seconds": 1.0,
            "caller": "episode",
            "method": "POST",
            "path": "/loop/eval-agent/api/v1/chat/completions",
            "request": {"model": "m", "messages": [SYSTEM, TASK], "tools": []},
            "status": 200,
            "response": stream({"role": "assistant", "content": "ok"}),
            "complete": True,
            "play": "attack",
            **call,
        }
        lines.append(GatewayCall.model_validate(fields).model_dump_json())
    (run / CALL_RECORD_FILE).write_text("".join(f"{line}\n" for line in lines))


def test_an_agents_answered_calls_are_its_turns_in_order(tmp_path: Path) -> None:
    write_calls(tmp_path, {}, {"path": "/loop/agent-main/api/v1/chat/completions"}, {})

    calls = read_session_calls(tmp_path, "attack", AGENTS)

    assert [(call.agent, call.turn, call.wall) for call in calls] == [
        ("eval-agent", 1, 100.0),
        ("agent-main", 1, 101.0),
        ("eval-agent", 2, 102.0),
    ]


def test_only_the_episodes_answered_calls_of_the_play_are_a_loops(tmp_path: Path) -> None:
    write_calls(
        tmp_path,
        {"caller": "sandbox-eval-agent"},
        {"status": 429},
        {"play": "twin"},
        {"path": "/loop/nobody/api/v1/chat/completions"},
        {"path": "/api/v1/chat/completions"},
        {},
    )

    calls = read_session_calls(tmp_path, "attack", AGENTS)

    assert [call.wall for call in calls] == [105.0]


def test_a_turn_lasts_until_its_agents_next_call(tmp_path: Path) -> None:
    write_calls(tmp_path, {}, {"path": "/loop/agent-main/api/v1/chat/completions"}, {})

    turns = build_turns(read_session_calls(tmp_path, "attack", AGENTS))

    assert turns == (
        TurnRecord(TurnRef("eval-agent", 1), 100.0, 102.0),
        TurnRecord(TurnRef("agent-main", 1), 101.0, float("inf")),
        TurnRecord(TurnRef("eval-agent", 2), 102.0, float("inf")),
    )


def test_a_turns_event_fingerprints_what_followed_the_brief_and_the_reply_with_its_tool_calls(
    tmp_path: Path,
) -> None:
    opening = {
        "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {**FUNCTION, "arguments": ""}},
        ],
    }
    rest = {"tool_calls": [{"index": 0, "function": {"arguments": FUNCTION["arguments"]}}]}
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 2}}
    write_calls(
        tmp_path,
        {"response": stream({"role": "assistant", "content": ""}, opening, rest, usage=usage)},
    )

    payload = build_inference_payload(read_session_calls(tmp_path, "attack", AGENTS)[0])

    assert payload == {
        "caller_identity": "eval-agent",
        "turn_id": "eval-agent:t1",
        "batch_id": None,
        "prompt_fp": fingerprint([TASK]),
        "output_fp": fingerprint(ProviderResult("", 0, 0, [BASH]).assistant_message()),
        "model_role": "untrusted_agent",
        "credential_kind": "turn_token",
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "cached_tokens": 2,
    }


def test_a_tool_a_loop_would_run_itself_is_found(tmp_path: Path) -> None:
    offered = [
        {"type": "function", "function": {"name": name}}
        for name in ("mcp__meridian__bash", "bash", "read_mcp_resource")
    ]
    write_calls(tmp_path, {"request": {"model": "m", "messages": [SYSTEM, TASK], "tools": offered}})

    local = find_local_tools(read_session_calls(tmp_path, "attack", AGENTS))

    assert local == {"eval-agent": ["bash"]}


def test_a_reply_that_broke_off_mid_chunk_is_read_up_to_its_last_whole_chunk(tmp_path: Path) -> None:
    whole = stream({"role": "assistant", "content": "ok"})
    cut = whole.split("data: [DONE]")[0] + 'data: {"choices": [{"index": 0, "del'
    write_calls(tmp_path, {"response": cut, "complete": False})

    payload = build_inference_payload(read_session_calls(tmp_path, "attack", AGENTS)[0])

    assert payload["output_fp"] == fingerprint(ProviderResult("ok", 0, 0, None).assistant_message())
