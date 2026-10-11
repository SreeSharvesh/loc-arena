"""Everything said in a run, where it went, and who read it.

Something is said by a bus message, a scratch note, or a post the environment makes (Slack, a pull request,
a review, a ticket, a wiki page). A read is a later prompt that holds the text, found by searching every later
model call for the text's longest plain stretch; a monitor also counts as a reader when a verdict of its sits
on what was said. A scratch note's ``shared_surface`` record is the same act as the note, so it is not listed
again.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from loc_arena.explorer.page import CommunicationView, ReadersView, StepView
from loc_arena.explorer.settings import ExplorerSettings
from loc_arena.logging_.transcript_lanes import WORLD

# What kind of thing was said: the page counts, filters and styles communication by these.
MESSAGE_KIND: Final = "message"
SCRATCH_KIND: Final = "scratch"
REFUSED_KIND: Final = "refused"  # a send the board turned down: tried, never delivered
ENVIRONMENT_KIND: Final = "environment"
SCRATCH_TOOL: Final = "write_scratch"
MESSAGE_TOOLS: Final = frozenset({"send", "message", "send_message"})  # the board's, and earlier runs'
MESSAGE_EVENT: Final = "message"
EVERYONE: Final = "team"  # the audience of a message with no single recipient
SCRATCH_AUDIENCE: Final = "scratch"
MONITOR_SUFFIX: Final = "_monitor"  # a monitor's own model calls run under its name, e.g. action_monitor
# Text that survives being quoted into a prompt, where line breaks may be escaped and quotes doubled.
PLAIN_CHARACTERS: Final = r"[A-Za-z0-9 ,.:;()'_/+=-]"


@dataclass(frozen=True)
class Utterance:
    """Something said, before its readers are known."""

    kind: str
    channel: str | None
    step: str
    sender: str
    audience: str
    text: str
    scores: tuple[float, ...]
    covert: tuple[str, ...]
    judged_by: tuple[str, ...]


@dataclass(frozen=True)
class Said:
    """Who said it and how the monitors took it: what every utterance in one tool call or event shares."""

    step: str
    sender: str
    scores: tuple[float, ...]
    covert: tuple[str, ...]
    judged_by: tuple[str, ...]


def extract_tool_utterance(
    said: Said,
    function: str,
    arguments: Mapping[str, object],
    refused: bool = False,
) -> Utterance | None:
    """A scratch note or a message sent through a tool call, else None; a refused send is kind `refused`."""
    if function == SCRATCH_TOOL:
        return _make_utterance(said, SCRATCH_KIND, None, SCRATCH_AUDIENCE, _read_text(arguments, "content"))
    if function in MESSAGE_TOOLS:
        audience = _read_text(arguments, "to", "recipient", "target") or EVERYONE
        kind = REFUSED_KIND if refused else MESSAGE_KIND
        return _make_utterance(said, kind, None, audience, _read_text(arguments, "body", "text"))
    return None


def extract_event_utterance(
    said: Said,
    kind: str,
    target: str | None,
    payload: Mapping[str, object],
) -> Utterance | None:
    """A bus message or a post by the environment, else None."""
    if kind == MESSAGE_EVENT:
        return _make_utterance(
            said,
            MESSAGE_KIND,
            None,
            target or _read_text(payload, "to") or EVERYONE,
            _read_text(payload, "body", "text"),
        )
    number = payload.get("number", "?")
    repository = _read_text(payload, "target_repo")
    posts = {
        "slack": (f"#{_read_text(payload, 'channel') or 'slack'}", _read_text(payload, "text")),
        "pr": (
            f"PR #{number}" + (f" in {repository}" if repository else ""),
            _read_pull_request_text(payload),
        ),
        "review": (
            f"PR #{number}",
            _read_text(payload, "reason") or ("approves" if payload.get("approved") else "blocks"),
        ),
        "ticket": (
            _read_text(payload, "ticket") or "ticket",
            f"state: {_read_text(payload, 'state') or '?'}",
        ),
        "wiki": (_read_text(payload, "page") or "wiki", _read_text(payload, "text", "body")),
    }
    if kind not in posts:
        return None
    audience, text = posts[kind]
    return _make_utterance(said, ENVIRONMENT_KIND, kind, audience, text)


def choose_probe(text: str, settings: ExplorerSettings) -> str | None:
    """The longest plain stretch of `text`, capped, or None when no stretch is long enough to trace."""
    pattern = re.compile(f"{PLAIN_CHARACTERS}{{{settings.read_probe_characters},}}")
    stretches = sorted((stretch.strip() for stretch in pattern.findall(text)), key=len, reverse=True)
    if not stretches or len(stretches[0]) < settings.read_probe_characters:
        return None
    return stretches[0][: settings.read_probe_cap]


def find_readers(
    utterance: Utterance,
    steps: Sequence[StepView],
    monitor_names: Collection[str],
    settings: ExplorerSettings,
) -> ReadersView:
    """Who read `utterance`: every model call after it, in `steps` order, whose prompt holds its text."""
    probe = choose_probe(utterance.text, settings)
    agents: dict[str, None] = {}
    rereads: dict[str, None] = {}
    monitors = dict.fromkeys(utterance.judged_by)
    others: dict[str, None] = {}
    start = next(i for i, step in enumerate(steps) if step.id == utterance.step) + 1
    if probe is None:
        return ReadersView(traced=False, agents=(), rereads=(), monitors=tuple(monitors), others=())
    for step in steps[start:]:
        for call in step.calls:
            if probe not in call.prompt:
                continue
            if call.identity in monitor_names or call.identity.endswith(MONITOR_SUFFIX):
                monitors[call.identity] = None
            elif step.agent != WORLD and call.identity == step.agent:
                (rereads if step.agent == utterance.sender else agents)[step.id] = None
            else:
                others[call.identity] = None
    return ReadersView(
        traced=True,
        agents=tuple(agents),
        rereads=tuple(rereads),
        monitors=tuple(monitors),
        others=tuple(others),
    )


def resolve_communication(
    utterances: Sequence[Utterance],
    steps: Sequence[StepView],
    monitor_names: Collection[str],
    settings: ExplorerSettings,
) -> tuple[CommunicationView, ...]:
    """Each utterance with its readers, in the order the steps ran."""
    order = {step.id: i for i, step in enumerate(steps)}
    return tuple(
        CommunicationView(
            kind=u.kind,
            channel=u.channel,
            step=u.step,
            sender=u.sender,
            audience=u.audience,
            text=u.text,
            scores=u.scores,
            covert=u.covert,
            readers=find_readers(u, steps, monitor_names, settings),
        )
        for u in sorted(utterances, key=lambda u: order[u.step])
    )


def _make_utterance(said: Said, kind: str, channel: str | None, audience: str, text: str) -> Utterance:
    return Utterance(
        kind=kind,
        channel=channel,
        step=said.step,
        sender=said.sender,
        audience=audience,
        text=text,
        scores=said.scores,
        covert=said.covert,
        judged_by=said.judged_by,
    )


def _read_text(values: Mapping[str, object], *keys: str) -> str:
    """The first of `keys` that holds a value, as text."""
    return next((str(values[key]) for key in keys if values.get(key) not in (None, "")), "")


def _read_pull_request_text(payload: Mapping[str, object]) -> str:
    return "\n".join(
        part for part in (_read_text(payload, "pr_title"), _read_text(payload, "pr_body")) if part
    ) or _read_text(payload, "diff")
