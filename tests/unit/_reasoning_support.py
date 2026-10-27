"""A stack run's bundle with one model call per play, and the gateway's record of the replies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from inspect_ai.event import ModelEvent
from inspect_ai.log import read_eval_log
from inspect_ai.model import ContentReasoning
from loc_arena.config import load_run_config
from loc_arena.gateway.core import GenerateRequest, output_fingerprint
from loc_arena.gateway.model_gateway import GatewayCall
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.logging_.inspect_export import EpisodeExport, write_run_eval

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
RUN_PLAY = "episode"  # the run's own play, whose sealed log is the bundle's; the twin's is in its folder


def write_bundle(bundle: Path, replies: Mapping[str, str]) -> None:
    write_run_eval(
        bundle / f"{bundle.name}.eval",
        run_name=bundle.name,
        config=CONFIG,
        mode="attack",
        seed=1,
        scores={},
        episodes=[_play_one_model_call(bundle, play, reply) for play, reply in replies.items()],
    )


def write_call_log(call_log: Path, recorded: Sequence[tuple[str, str]]) -> None:
    calls = (
        GatewayCall(
            started_at=0.0,
            seconds=1.0,
            caller="episode",
            method="POST",
            path="/api/v1/chat/completions",
            request={},
            status=200,
            response={"choices": [{"message": {"content": text, "reasoning": reasoning}}]},
            complete=True,
        )
        for text, reasoning in recorded
    )
    call_log.parent.mkdir(parents=True, exist_ok=True)
    call_log.write_text("".join(f"{call.model_dump_json()}\n" for call in calls))


def read_shown_reasoning(bundle: Path) -> dict[str, tuple[list[str], str]]:
    log = read_eval_log(str(bundle / f"{bundle.name}.eval"), resolve_attachments=True)
    shown: dict[str, tuple[list[str], str]] = {}
    for sample in log.samples or []:
        (model,) = [event for event in sample.events if isinstance(event, ModelEvent)]
        said = [part.reasoning for part in model.output.message.content if isinstance(part, ContentReasoning)]
        shown[str(sample.id)] = (said, model.output.completion)
    return shown


def _play_one_model_call(bundle: Path, play: str, reply: str) -> EpisodeExport:
    sealed_path = (bundle if play == RUN_PLAY else bundle / play) / "events.sealed.jsonl"
    trace = AgentTrace(wall_clock=lambda: 1_700_000_000.0)
    sealed = AppendOnlyLog(sealed_path, play, on_append=trace.on_sealed_append)
    chat = GenerateRequest(messages=[{"role": "user", "content": "go"}], caller_identity="agent-main")
    with trace.turn("agent-main", 0):
        record = sealed.append(
            Event(
                episode_id=play,
                seq=sealed.next_seq(),
                ts=0.0,
                actor_uid="agent-main",
                actor_role="untrusted",
                kind="inference_call",
                payload={"output_fp": output_fingerprint(chat, reply, None)},
            ),
        )
        trace.on_model_call(
            identity="agent-main",
            role="untrusted_agent",
            model_input="go",
            output=reply,
            sealed_seq=record.seq,
        )
    return EpisodeExport(play, trace.finish(last_sealed_seq=sealed.last_seq), sealed_path, ("agent-main",))
