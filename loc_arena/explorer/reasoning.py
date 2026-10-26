"""The model's reasoning on each model call of a run, for the people reading it, from the gateway's call log.

A stack run's gateway records every reply as OpenRouter returned it, reasoning included, in a call log beside
the run's bundle (``locate_call_log``); the sealed log keeps only each reply's fingerprint. A recorded reply
belongs to the sealed ``inference_call`` whose ``output_fp`` it fingerprints to, as the gateway computes it:
the assistant message, or the bare text for a ``prompt`` or batch call. Calls with the same reply are paired
in order, each sealed event with the first recorded call not yet paired. The call log covers the run's honest
twin too, which played after the run, so the twin's calls pair after all of the run's. Only the explorer reads
it: no monitor input holds reasoning.
"""

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
    """The model's reasoning by the sealed seq of its call, for each call it returned some on.

    The run's own play, or the honest twin's when ``twin`` names its folder in the bundle, paired after the
    run's so a reply both plays share goes to the run first. Nothing when no call log sits where a stack run
    leaves it: an in-process run, or a bundle moved away.
    """
    call_log = locate_call_log(bundle.parent.parent, bundle.name.rpartition("__")[2])
    if not call_log.exists():
        return {}
    waiting: dict[str, deque[tuple[int, str | None]]] = defaultdict(deque)
    for index, line in enumerate(call_log.read_text(encoding="utf-8").splitlines()):
        try:
            call = GatewayCall.model_validate_json(line)
            if not isinstance(call.response, dict) or not call.response.get("choices"):
                continue  # a refused or failed call, or a stream: no reply to pair
            message = _RecordedCompletion.model_validate(call.response).choices[0].message
        except ValidationError:
            continue  # a line cut off when the run stopped, or a reply of another shape: nothing to show
        text = message.content or ""
        reply = ProviderResult(text, 0, 0, message.tool_calls or None).assistant_message()
        for output_fp in {fingerprint(reply), *([] if message.tool_calls else [fingerprint(text)])}:
            waiting[output_fp].append((index, message.reasoning or None))
    paired: set[int] = set()
    plays = [bundle, twin] if twin else [bundle]
    reasoning: dict[int, str] = {}
    for play in plays:
        reasoning = {}  # only the last play's is kept: an earlier play only takes its calls first
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
