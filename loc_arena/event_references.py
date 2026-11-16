"""Which earlier event each event of a play refers to by an id it names or bears.

An id is a ticket's, a pull request's by its repository, a note's, a job's or a chat message's. An event
refers to the first and the last earlier event that bore an id it names.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from loc_arena.logging_.events import Event

_TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_PULL_REQUEST = re.compile(r"\b(?:PR|pull request)\s*#?(\d+)\b|(?<![\w/&])#(\d+)\b", re.IGNORECASE)
_REPOSITORY_PULL_REQUEST = re.compile(r"(?<![\w/.-])([a-z][\w.-]*#\d+)\b")  # meridian-serving#2
_JOB = re.compile(r"\bjob-[0-9a-z-]+\b")


def refer_by_id(sealed: Sequence[Event], words: Mapping[int, str]) -> dict[int, list[tuple[int, str]]]:
    """Each event's references by id, as the earlier seq and the id, over the play's ``sealed`` log."""
    bearing: dict[str, list[int]] = {}  # the events that bore each id so far
    refs: dict[int, list[tuple[int, str]]] = {}
    for event in sealed:
        resolved = (_resolve(name, event, bearing) for name in _ids_named(event, words.get(event.seq, "")))
        refs[event.seq] = [
            (seq, name)
            for name in sorted(name for name in resolved if name)
            for seq in dict.fromkeys(bearing[name][:1] + bearing[name][-1:])
        ]
        for name in _ids_borne(event):
            bearing.setdefault(name, []).append(event.seq)
    return refs


def job_of(event: Event) -> str | None:
    """The job an event is of: a job's state, or the action that submitted it."""
    if event.kind == "job":
        return str(event.payload.get("job_id") or "") or None
    result = event.result if isinstance(event.result, dict) else {}
    return (str(result.get("id") or "") or None) if event.tool == "submit_job" else None


def _resolve(name: str, event: Event, bearing: Mapping[str, list[int]]) -> str | None:
    """The id ``name`` stands for, or none when no earlier event bore it.

    A bare pull request number is of the event's own repository, or of the one repository that bears it.
    """
    if not name.startswith("#"):
        return name if bearing.get(name) else None
    own = f"{event.target_id}{name}"
    candidates = [borne for borne in bearing if borne.endswith(name) and borne.partition("#")[0]]
    return own if own in bearing else candidates[0] if len(candidates) == 1 else None


_TARGETS = {  # the id an action of each tool names by its target
    **dict.fromkeys(("edit_ticket", "comment_on_ticket"), "{}"),
    **dict.fromkeys(("read_note", "write_note", "delete_note"), "note:{}"),
    **dict.fromkeys(("cancel_job", "hold_job", "release_job", "operate_job"), "{}"),
}


def _ids_borne(event: Event) -> set[str]:
    """The ids an event bears itself.

    The ticket it made or changed, its pull request by its repository, the note it wrote, its job and message.
    """
    borne = set()
    result = event.result if isinstance(event.result, dict) else {}
    if event.tool == "create_ticket" and result.get("id"):  # a refused one made none
        borne.add(str(result["id"]))
    if event.tool in ("edit_ticket", "comment_on_ticket") and event.target_id:
        borne.add(event.target_id)
    number = event.payload.get("number") or event.payload.get("pr_number")
    if event.kind in ("pr", "commit", "review") and number is not None:
        borne.add(f"{event.target_id}#{number}")
    if event.tool == "write_note" and event.target_id:
        borne.add(f"note:{event.target_id}")
    if job := job_of(event):
        borne.add(job)
    if event.kind in ("message", "slack") and event.payload.get("message_id") is not None:
        borne.add(f"message:{event.payload['message_id']}")
    return borne


def _ids_named(event: Event, words: str) -> set[str]:
    """The ids an event refers to, which an earlier event bore.

    Those its words name, the ticket, note or job an action of it targets, the message a reply answers, and
    those it bears itself: an earlier event of the same pull request, note or job bore them.
    """
    named = set(_TICKET.findall(words)) | set(_JOB.findall(words))
    named |= {f"#{first or second}" for first, second in _PULL_REQUEST.findall(words)}
    named |= set(_REPOSITORY_PULL_REQUEST.findall(words))
    if (target := _TARGETS.get(event.tool or "")) and event.target_id:
        named.add(target.format(event.target_id))
    if event.kind in ("message", "slack") and event.payload.get("thread") is not None:
        named.add(f"message:{event.payload['thread']}")
    return named | _ids_borne(event)  # a review, merge or job's later state refers to what bore its id before
