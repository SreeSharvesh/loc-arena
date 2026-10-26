from __future__ import annotations

from pathlib import Path

from loc_arena.gateway.core import GenerateRequest, output_fingerprint
from loc_arena.gateway.model_gateway import GatewayCall
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.logging_.reasoning import read_reasoning


def test_a_reply_paired_with_a_chat_call_is_not_paired_again_by_its_bare_text(tmp_path: Path) -> None:
    bundle = tmp_path / "logs" / "run" / "run__abc123"
    sealed = AppendOnlyLog(bundle / "events.sealed.jsonl", "episode")
    chat = GenerateRequest(messages=[{"role": "user", "content": "go"}], caller_identity="agent-main")
    batch = GenerateRequest(prompt="go", caller_identity="coworker")
    for seq, request in enumerate((chat, batch), start=1):
        payload = {"output_fp": output_fingerprint(request, "", None)}
        sealed.append(Event("episode", seq, 0.0, "gateway", "gateway", "inference_call", payload))
    call_log = tmp_path / "logs" / "gateway" / "locarena-abc123.calls.jsonl"
    call_log.parent.mkdir()
    call_log.write_text(
        "".join(
            GatewayCall(
                started_at=0.0,
                seconds=1.0,
                caller="episode",
                method="POST",
                path="/api/v1/chat/completions",
                request={},
                status=200,
                response={"choices": [{"message": {"content": "", "reasoning": reasoning}}]},
                complete=True,
            ).model_dump_json()
            + "\n"
            for reasoning in ("the agent's", "the coworker's")
        ),
    )

    reasoning = read_reasoning(bundle)

    assert reasoning == {1: "the agent's", 2: "the coworker's"}


def test_a_cut_off_or_odd_call_log_line_is_skipped_and_the_lines_around_it_still_pair(tmp_path: Path) -> None:
    bundle = tmp_path / "logs" / "run" / "run__abc123"
    sealed = AppendOnlyLog(bundle / "events.sealed.jsonl", "episode")
    chat = GenerateRequest(messages=[{"role": "user", "content": "go"}], caller_identity="agent-main")
    payload = {"output_fp": output_fingerprint(chat, "", None)}
    sealed.append(Event("episode", 1, 0.0, "gateway", "gateway", "inference_call", payload))
    recorded = GatewayCall(
        started_at=0.0,
        seconds=1.0,
        caller="episode",
        method="POST",
        path="/api/v1/chat/completions",
        request={},
        status=200,
        response={"choices": [{"message": {"content": "", "reasoning": "kept"}}]},
        complete=True,
    )
    odd = recorded.model_copy(update={"response": {"choices": [{"message": "not a message"}]}})
    call_log = tmp_path / "logs" / "gateway" / "locarena-abc123.calls.jsonl"
    call_log.parent.mkdir()
    lines = (odd.model_dump_json(), recorded.model_dump_json(), recorded.model_dump_json()[:40])
    call_log.write_text("\n".join(lines))

    assert read_reasoning(bundle) == {1: "kept"}
