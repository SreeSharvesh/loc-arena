"""Connect captured assistant calls to their results, and recorded actions to the turn that issued them."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from run_insights.models import Record


@dataclass(frozen=True)
class SessionLinks:
    """The assistant turn for a source, and each turn's unambiguously matched tool results."""

    turns: dict[str, str]
    results: dict[str, dict[str, str]]


def _signature(play: str, actor: str, tool: object, arguments: object) -> tuple[str, str, str, str] | None:
    """A call as both logs see it: who made it, the tool's bare name and its exact arguments."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return None
    return play, actor, tool.rsplit("__", 1)[-1], json.dumps(arguments, sort_keys=True)


def link_session_calls(records: Sequence[Record]) -> SessionLinks:  # noqa: C901 - one pass over three record shapes
    """Link within one actor and play only; ambiguous or absent identifiers stay unlinked.

    A recorded action joins the turn whose one call has the same tool and arguments, and only when exactly one
    action and one call share them.
    """
    calls: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    issued: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    actions: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    results: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    turns: dict[str, str] = {}
    for record in records:
        message = record.raw.get("message")
        payload = record.raw.get("payload")
        if record.stream == "sealed" and record.kind == "action" and isinstance(payload, dict):
            key = _signature(record.play, record.actor, record.tool, payload.get("args"))
            if key:
                actions[key].append(record.id)
        if record.stream != "session" or not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            turns[record.id] = record.id
            content = message.get("content")
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    call_id = part.get("id")
                    if isinstance(call_id, str) and call_id:
                        calls[(record.play, record.actor, call_id)].append(record.id)
                    key = _signature(record.play, record.actor, part.get("name"), part.get("arguments"))
                    if key:
                        issued[key].append(record.id)
        if message.get("role") == "toolResult":
            call_id = message.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                results[(record.play, record.actor, call_id)].append(record.id)
    matched: dict[str, dict[str, str]] = defaultdict(dict)
    for key, callers in calls.items():
        returned = results.get(key, [])
        if len(callers) == len(returned) == 1:
            turn, result = callers[0], returned[0]
            turns[result] = turn
            matched[turn][key[2]] = result
    for key, done in actions.items():
        if len(done) == len(issued.get(key, [])) == 1:
            turns[done[0]] = issued[key][0]
    return SessionLinks(turns=turns, results=dict(matched))
