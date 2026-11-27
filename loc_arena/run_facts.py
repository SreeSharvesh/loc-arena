"""What a reading may state about a run, from its records alone: its facts, its events' words and its names.

A reading states a number only as one of these facts, quotes only words an event holds, and names in
backticks only what the records hold (``reading_check``). The command gathers all three before the reader
starts and gives the reader the facts and the words, so the reader is never their source.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import get_args

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from loc_arena.explorer.scores import load_scores
from loc_arena.gateway.model_gateway import PHASE_PLAYS
from loc_arena.logging_.events import Event, EventKind, read_events
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE
from loc_arena.monitors.registry import MonitorVerdict
from loc_arena.own_sessions import SESSION_ENDS_FILE, EndReason
from loc_arena.session_events import read_reply_text, read_session_calls
from loc_arena.session_slots import SESSIONS_FILE

type Fact = str | int | float | bool | list[str]
MIRROR = "mirror"  # a mirror event's id is `<play>-mirror:<seq>`; a sealed event's is `<play>:<seq>`
_TOKEN = re.compile(r"[\w./@#:+-]+")
_ONE_NAME = re.compile(r"[A-Za-z_][\w./:+-]{0,63}")  # a fact's string: one name, never free text
_CODE_ARGUMENTS = frozenset({"command", "content", "old_string", "new_string", "code", "script", "patch"})


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Scope(_Record):
    open_pr: tuple[str, ...] = ()  # the repositories it may open pull requests on: ``*`` for every one


class _Agent(_Record):
    id: str
    kind: str | None = None  # orchestrator or subagent
    spawned_by: str | None = None  # whom it reports to
    covert: tuple[str, ...] | None = None  # the milestones its covert brief serves
    scope: _Scope = _Scope()


class _Episode(_Record):
    orchestrator_turn_budget: int | None = None  # each kind of agent's call budget
    subagent_turn_budget: int | None = None
    wall_clock_seconds: float | None = None  # the play's bound


class _Covert(_Record):
    target_identity: str  # the agent the covert objective is briefed to


class _Milestone(_Record):
    id: str


class _SideTask(_Record):
    milestones: tuple[_Milestone, ...]


class ResolvedConfig(_Record):
    """Of a run's ``config.resolved.yaml``, what its records are read with: its team, side task and bounds."""

    agents: tuple[_Agent, ...]
    covert: _Covert
    side_task: _SideTask
    episode: _Episode = _Episode()


class _SessionEnd(_Record):
    reason: EndReason


def read_resolved_config(run_directory: Path) -> ResolvedConfig:
    """The team, side task and bounds of the run in ``run_directory``."""
    return ResolvedConfig.model_validate(yaml.safe_load((run_directory / "config.resolved.yaml").read_text()))


def read_session_ends(directory: Path) -> dict[str, EndReason]:
    """How each agent's session ended in the play in ``directory``; none before the play wrote it."""
    ends = directory / SESSION_ENDS_FILE
    recorded = TypeAdapter(dict[str, _SessionEnd]).validate_json(ends.read_text()) if ends.is_file() else {}
    return {agent: end.reason for agent, end in recorded.items()}


class EventWords(BaseModel):
    """One event as a reading cites it: who did what, and the words it holds, which a claim may quote."""

    model_config = ConfigDict(frozen=True)

    id: str
    actor: str
    kind: str
    tool: str | None
    target: str | None
    words: tuple[str, ...]


class RunEvidence(BaseModel):
    """A run's facts by key, its events by id, its records' names and the names asked about."""

    model_config = ConfigDict(frozen=True)

    facts: dict[str, Fact]
    events: dict[str, EventWords]
    names: frozenset[str]
    asked: frozenset[str] = frozenset()  # the names of the owner's question, which a claim may repeat


def name_plays(run_directory: Path) -> list[tuple[str, str]]:
    """Each play's name and directory: the run's mode for its first play, and ``twin`` for its honest twin."""
    plays = [(load_scores(run_directory / "scores.json").mode, EPISODE_PHASE), ("twin", TWIN_PHASE)]
    return [
        (play, phase) for play, phase in plays if (run_directory / phase / "events.sealed.jsonl").is_file()
    ]


class _SessionStart(_Record):
    session: str
    starter: str | None = None  # none on the line of its end


def read_session_starters(run_directory: Path) -> dict[str, str]:
    """Each session a play of the run started, with its starter: its own actor, on its starter's scope."""
    starters: dict[str, str] = {}
    for _, phase in name_plays(run_directory):
        record = run_directory / phase / SESSIONS_FILE
        for line in record.read_text().splitlines() if record.is_file() else ():
            start = _SessionStart.model_validate_json(line)
            starters |= {start.session: start.starter} if start.starter else {}
    return starters


def gather_evidence(run_directory: Path, word_fields: Sequence[str]) -> RunEvidence:
    """The facts, the words and the names of the graded run in ``run_directory``.

    An event's words are its payload's and its arguments' text in ``word_fields``, and for a model call of an
    agent's session the text of its model's reply.
    """
    scores = json.loads((run_directory / "scores.json").read_text())
    config = read_resolved_config(run_directory)
    graded, agents = load_scores(run_directory / "scores.json"), [agent.id for agent in config.agents]
    actors = [*agents, *read_session_starters(run_directory)]  # the agents, and each session they started
    milestones = [milestone.id for milestone in config.side_task.milestones]
    facts: dict[str, Fact] = {
        "scores.keys": sorted(scores),
        "scores.key_count": len(scores),
        "run.mode": str(scores["mode"]),
        "run.agents": agents,
        "run.covert_agent": config.covert.target_identity,
        "side_task.milestones": milestones,
        "side_task.milestone_count": len(milestones),
    }
    facts |= _agent_facts(config)
    _flatten("scores", scores, facts)
    events: dict[str, EventWords] = {}
    names = {
        *_name_keys(scores),
        *_name_keys(yaml.safe_load((run_directory / "config.resolved.yaml").read_text())),
        *agents,
    }
    for play, phase in name_plays(run_directory):
        sealed = list(read_events(run_directory / phase / "events.sealed.jsonl"))
        twin = graded.honest_twin.monitor_verdicts if graded.honest_twin else ()
        verdicts = graded.monitor_verdicts if phase == EPISODE_PHASE else twin
        facts |= _count_play(play, sealed, run_directory / phase, verdicts, graded.safety.audit_threshold)
        made = [  # a subagent, <agent>.<n>, makes what it makes for its agent
            (actor, key)
            for event in sealed
            if (actor := event.actor_uid.partition(".")[0]) in actors
            for key in _made(event)
        ]
        team = Counter(key for _, key in made)  # the agents' alone, the monitors' and coworkers' apart
        facts |= {f"{play}.team.{key}": count for key, count in team.items()}
        facts |= {
            f"{play}.team.{key}.actors": sorted({actor for actor, of in made if of == key}) for key in team
        }
        facts |= {
            f"{play}.team.{key}.actor_count": len({actor for actor, of in made if of == key}) for key in team
        }
        replies = {
            f"{call.agent}:t{call.turn}": read_reply_text(call.call.response)
            for call in read_session_calls(run_directory, PHASE_PLAYS[phase], actors)
        }
        mirror = list(read_events(run_directory / phase / "events.mirror.jsonl"))
        for prefix, log in ((play, sealed), (f"{play}-{MIRROR}", mirror)):
            for event in log:
                cited = _describe(f"{prefix}:{event.seq}", event, word_fields, replies)
                events[cited.id] = cited
                names.update(_name_event(event, word_fields))
    _count_zeros(facts, [play for play, _ in name_plays(run_directory)], agents)
    files = (str(path.relative_to(run_directory)) for path in run_directory.rglob("*") if path.is_file())
    names.update(ending for file in files for ending in _endings(file))
    names.update(_name_facts(facts))  # every play's facts included
    return RunEvidence(facts=facts, events=events, names=frozenset(names))


def write_evidence(evidence: RunEvidence, directory: Path) -> None:
    """Write the facts (``facts.json``) and each event with words (``words.jsonl``) into ``directory``."""
    (directory / "facts.json").write_text(json.dumps(evidence.facts, indent=1, sort_keys=True))
    worded = (event.model_dump_json() for event in evidence.events.values() if event.words)
    (directory / "words.jsonl").write_text("".join(f"{line}\n" for line in worded))


def _agent_facts(config: ResolvedConfig) -> dict[str, Fact]:
    """Each agent's kind, whom it reports to, its covert brief's milestones and its repositories."""
    facts: dict[str, Fact] = {}
    for agent in config.agents:
        facts |= {
            f"run.agent.{agent.id}.kind": agent.kind or "none",
            f"run.agent.{agent.id}.reports_to": agent.spawned_by or "none",
            f"run.agent.{agent.id}.covert": list(agent.covert or ()),
            f"run.agent.{agent.id}.repositories": list(agent.scope.open_pr),
        }
    return facts


def _count_play(
    play: str,
    sealed: Sequence[Event],
    directory: Path,
    verdicts: Iterable[MonitorVerdict],
    threshold: float,
) -> dict[str, Fact]:
    """One play's counts: events by kind, tool and actor, its sessions' ends, and the monitors' verdicts."""
    counted = Counter[str]()
    actors: dict[str, set[str]] = {}  # who made each kind of event and used each tool
    for event in sealed:
        made = _made(event)
        counted.update([*made, *(f"agent.{event.actor_uid}.{key}" for key in made)])
        for key in made:
            actors.setdefault(key, set()).add(event.actor_uid)
    facts: dict[str, Fact] = {f"{play}.events": len(sealed)}
    facts |= {f"{play}.{key}": count for key, count in counted.items()}
    facts |= {f"{play}.{key}.actors": sorted(names) for key, names in actors.items()}
    facts |= {f"{play}.{key}.actor_count": len(names) for key, names in actors.items()}
    reasons = read_session_ends(directory)
    facts |= {f"{play}.session.{agent}": reason for agent, reason in reasons.items()}
    facts |= {f"{play}.sessions.{reason}": count for reason, count in Counter(reasons.values()).items()}
    scored: dict[str, list[float]] = {}
    by_event: dict[str, float] = {}  # each scored mirror event's score from each monitor, its taps' highest
    for verdict in verdicts:
        scored.setdefault(verdict.monitor, []).append(verdict.score)
        if verdict.target_seq is not None:
            key = f"{play}.verdict.{verdict.target_seq}.{verdict.monitor}"
            by_event[key] = max(verdict.score, by_event.get(key, 0.0))
    facts |= by_event
    for monitor, scores in scored.items():
        facts |= {
            f"{play}.monitor.{monitor}.verdicts": len(scores),
            f"{play}.monitor.{monitor}.zero": sum(score == 0 for score in scores),
            f"{play}.monitor.{monitor}.below": sum(0 < score <= threshold for score in scores),
            f"{play}.monitor.{monitor}.flagged": sum(score > threshold for score in scores),
            f"{play}.monitor.{monitor}.highest": max(scores),
        }
    facts[f"{play}.flagged"] = sum(score > threshold for scores in scored.values() for score in scores)
    return facts


def _made(event: Event) -> list[str]:
    """What an event counts toward: its kind, its tool if any, and a message's kind (a post, a reply)."""
    said = event.payload.get("message_kind") if event.kind == "message" else None
    return [
        f"kind.{event.kind}",
        *([f"tool.{event.tool}"] if event.tool else []),
        *([f"message.{said}"] if said else []),
    ]


def _count_zeros(facts: dict[str, Fact], plays: Sequence[str], agents: Sequence[str]) -> None:
    """A zero for each kind of event, and each tool and kind of message a play's records show, not seen.

    A tool no play's records show has no zero: many tools leave no event of their name (a read leaves none, a
    chat post leaves a message), so its zero would be no fact.
    """
    seen = {key.split(".", 1)[1] for key in facts if re.fullmatch(r"[^.]+\.(kind|tool|message)\.[^.]+", key)}
    for play in plays:
        for key in seen | {f"kind.{kind}" for kind in get_args(EventKind)}:
            for counted in (f"{play}.{key}", f"{play}.team.{key}"):
                facts |= {
                    counted: facts.get(counted, 0),
                    f"{counted}.actor_count": facts.get(f"{counted}.actor_count", 0),
                }
                facts.setdefault(f"{counted}.actors", [])
            for agent in agents:
                facts.setdefault(f"{play}.agent.{agent}.{key}", 0)
        for reason in get_args(EndReason):
            facts.setdefault(f"{play}.sessions.{reason}", 0)


def _describe(cite: str, event: Event, word_fields: Sequence[str], replies: Mapping[str, str]) -> EventWords:
    """``event`` as ``cite`` names it: its words are its text under ``word_fields`` and its model's reply."""
    arguments = event.payload.get("args")
    texts = [
        value
        for fields in (event.payload, arguments if isinstance(arguments, dict) else {})
        for name, value in fields.items()
        if name in word_fields and isinstance(value, str) and value.strip()
    ]
    if event.kind == "inference_call" and (
        reply := replies.get(str(event.payload.get("turn_id")), "").strip()
    ):
        texts.append(reply)
    return EventWords(
        id=cite,
        actor=event.actor_uid,
        kind=event.kind,
        tool=event.tool,
        target=event.target_id,
        words=tuple(texts),
    )


def _flatten(prefix: str, value: JsonValue, into: dict[str, Fact]) -> None:
    """Each scalar of ``value`` under its dotted key; each list by its length, and a list of scalars whole.

    A string is a fact only when it is one name: free text, such as an error a grade recorded, would be shown
    unchecked wherever a claim filled it in, so it is none.
    """
    if isinstance(value, dict):
        for key, inner in value.items():
            _flatten(f"{prefix}.{key}", inner, into)
    elif isinstance(value, list):
        if all(not isinstance(item, dict | list) and _is_factual(item) for item in value):
            into[prefix] = [str(item) for item in value]
        into[f"{prefix}.count"] = len(value)
    elif _is_factual(value):
        into[prefix] = "none" if value is None else value


def _is_factual(value: JsonValue) -> bool:
    """Whether a scalar may stand as a fact: a number, a truth value, nothing, or one name."""
    return not isinstance(value, str) or bool(_ONE_NAME.fullmatch(value))


def _name_keys(value: JsonValue) -> Iterator[str]:
    """Every key of ``value``, at any depth: a structured name, never free text."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from _name_keys(inner)
    elif isinstance(value, list):
        for item in value:
            yield from _name_keys(item)


def _name_facts(facts: Mapping[str, Fact]) -> Iterator[str]:
    """Each fact's key, and each value of it that is one name: an agent, a milestone, a model, a reason."""
    for key, value in facts.items():
        yield key
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item and not any(character.isspace() for character in item):
                yield item


def _name_event(event: Event, word_fields: Sequence[str]) -> Iterator[str]:
    """The structured names of ``event``: its actor, kind, tool and target, and each argument of one name.

    An argument that holds words, a command or code is free text, and names nothing.
    """
    yield from (str(field) for field in (event.actor_uid, event.kind, event.tool, event.target_id) if field)
    arguments = event.payload.get("args")
    for key, value in arguments.items() if isinstance(arguments, dict) else ():
        if key not in word_fields and key not in _CODE_ARGUMENTS:
            yield from _name_values(value)


def _name_values(value: JsonValue) -> Iterator[str]:
    """Each string of ``value`` that is one name, at any depth, with each ending of it as a path."""
    if isinstance(value, dict):
        for inner in value.values():
            yield from _name_values(inner)
    elif isinstance(value, list):
        for item in value:
            yield from _name_values(item)
    elif isinstance(value, str) and value and not any(character.isspace() for character in value):
        yield from _endings(value)


def _endings(path: str) -> Iterator[str]:
    """A path and each of its endings: `meridian_serving/http.py`, of a path that ends so."""
    parts = path.split("/")
    yield from ("/".join(parts[start:]) for start in range(len(parts)) if parts[start])


def find_names(value: object) -> Iterator[str]:
    """Every key of ``value``, and each name in its text: a word, an id or a path, and a path's endings."""
    if isinstance(value, Mapping):
        for key, inner in value.items():
            yield str(key)
            yield from find_names(inner)
    elif isinstance(value, Iterable) and not isinstance(value, str | bytes):
        for item in value:
            yield from find_names(item)
    elif value is not None:
        for token in _TOKEN.findall(str(value)):
            parts = token.split("/")  # a path by each of its endings too: `meridian_serving/http.py`
            yield from filter(None, ("/".join(parts[start:]) for start in range(len(parts))))  # never empty
