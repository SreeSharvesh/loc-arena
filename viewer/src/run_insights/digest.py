"""A run's attack play as one text a model reads in a single call: what each agent thought, did, got."""

from __future__ import annotations

import json

from pydantic import JsonValue

from run_insights.models import Record, RunData

REASONING, RESULT, EVENT, BRIEF = 800, 300, 300, 1500  # characters kept of each
# Each model call, each repository clone, and each tool call a service recorded, which the sessions hold.
_UNREAD = {"inference_call", "checkout", "action"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]} [... {len(text) - limit} more characters]"


def _flat(value: JsonValue) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _turn(record: Record) -> str | None:
    """One session record as a line: an assistant turn's reasoning, words and calls, or a tool's result."""
    message = record.raw.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    parts = [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []
    role, said = message.get("role"), []
    if role == "assistant":
        for part in parts:
            if part.get("type") == "thinking":
                said.append(f"thinks: {_clip(_flat(part.get('thinking', '')), REASONING)}")
            elif part.get("type") == "text":
                said.append(f"says: {_clip(_flat(part.get('text', '')), REASONING)}")
            elif part.get("type") == "toolCall":
                said.append(f"calls {part.get('name')}({_clip(_flat(part.get('arguments')), RESULT)})")
    elif role == "toolResult":
        text = " ".join(_flat(part.get("text", "")) for part in parts if part.get("type") == "text")
        error = " (error)" if message.get("isError") else ""
        said.append(f"result of {message.get('toolName')}{error}: {_clip(text, RESULT)}")
    elif role == "user":
        said.append(f"is told: {_clip(' '.join(_flat(part.get('text', '')) for part in parts), RESULT)}")
    return f"{record.actor} " + " | ".join(said) if said else None


def digest_run(run: RunData) -> tuple[str, dict[str, str]]:
    """The attack play's sessions in order, then the services' own records, with long text clipped.

    The honest twin, the mirror log (a copy of the sealed one) and the records listed in ``_UNREAD`` are
    left out. Each line opens with a short handle in brackets, such as ``[r12]``, which a citation names:
    a model copies a short handle faithfully where it shortens a long ID. Returns the text and the record ID
    of each handle.
    """
    lines = [f"# Run {run.name}", f"Outcome: {run.outcome.model_dump_json()}"]
    lines += [f"Score {name}: {_clip(_flat(value), 4000)}" for name, value in run.scores.items()]
    for agent in run.agents:
        lines += [f"## Agent {agent.id}", f"Brief: {_clip(agent.brief, BRIEF)}"]
        lines += [f"Covert brief: {_clip(agent.covert_brief, BRIEF)}"] if agent.covert_brief else []
    attack = [record for record in run.records if record.play == "attack"]
    sessions = [(record, line) for record in attack if record.stream == "session" and (line := _turn(record))]
    services = [
        (
            record,
            f"t={record.time:.0f} {record.actor} {record.kind}: "
            + _clip(_flat(record.raw.get("payload")), EVENT),
        )
        for record in attack
        if record.stream == "sealed" and record.kind not in _UNREAD
    ]
    handles: dict[str, str] = {}
    for title, entries in (
        ("Agent sessions, each in order", sessions),
        ("Service records, in order (t is seconds into the play)", services),
    ):
        lines.append(f"## {title}")
        for record, line in entries:
            handle = f"r{len(handles) + 1}"
            handles[handle] = record.id
            lines.append(f"[{handle}] {line}")
    return "\n".join(lines), handles
