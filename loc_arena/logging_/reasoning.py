"""The reasoning on each model call, from the gateway's call log; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ValidationError

from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.model_gateway import GatewayCall, locate_call_log
from loc_arena.logging_.events import fingerprint, read_events


class _RecordedMessage(BaseModel):
    """The reply message of a recorded chat completion: text, tool calls and the model's reasoning."""

    content: str | None = None
    tool_calls: list[dict[str, object]] | None = None
    reasoning: str | None = None


class _RecordedChoice(BaseModel):
    message: _RecordedMessage


class _RecordedCompletion(BaseModel):
    choices: list[_RecordedChoice]


def read_reasoning(bundle: Path, twin: Path | None = None) -> Mapping[int, str]:  # noqa: C901 - predates the complexity gate
    call_log = locate_call_log(bundle.parent.parent, bundle.name.rpartition("__")[2])
    if not call_log.exists():
        return {}
    waiting: dict[str, deque[tuple[int, str | None]]] = defaultdict(deque)
    for index, line in enumerate(call_log.read_text(encoding="utf-8").splitlines()):
        try:
            call = GatewayCall.model_validate_json(line)
            if not call.complete or not isinstance(call.response, dict) or not call.response.get("choices"):
                continue
            message = _RecordedCompletion.model_validate(call.response).choices[0].message
        except ValidationError:
            continue
        text = message.content or ""
        reply = ProviderResult(text, 0, 0, message.tool_calls or None).assistant_message()
        for output_fp in {fingerprint(reply), *([] if message.tool_calls else [fingerprint(text)])}:
            waiting[output_fp].append((index, message.reasoning or None))
    paired: set[int] = set()
    plays = [bundle, twin] if twin else [bundle]
    reasoning: dict[int, str] = {}
    for play in plays:
        reasoning = {}
        for event in read_events(play / "events.sealed.jsonl"):
            if event.kind != "inference_call":
                continue
            calls = waiting[event.payload["output_fp"]]
            while calls and calls[0][0] in paired:
                calls.popleft()
            if calls:
                index, said = calls.popleft()
                paired.add(index)
                if said:
                    reasoning[event.seq] = said
    return reasoning
