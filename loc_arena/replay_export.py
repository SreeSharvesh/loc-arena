"""The replay export: one JSON file per run that a visual page is built from, the raw records left unread.

``replay.json`` holds the run (its config, commit, team, models, budgets, bound, cost and where each trace
lives), both plays' events (time from play start, actor, channel, action, an agent's words in prose alone,
outcome, covert flag and the earlier events each refers to), the outcomes with their reasons, and the
taxonomy's instances and counts. Code builds it from the records; the reader's checked reading adds what code
cannot show, each part marked as the reader's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

from loc_arena.config import RunConfig
from loc_arena.explorer.scores import RunScores, load_scores
from loc_arena.logging_.events import Event
from loc_arena.reading_check import CheckedReading, KeptClaim, prose_lines
from loc_arena.recorded_events import EVENT_WALLS_FILE
from loc_arena.run_facts import MIRROR, RunEvidence, gather_evidence, name_plays
from loc_arena.run_identity import RUN_FILE, RunIdentity
from loc_arena.run_reader import READINGS, read_run, settle_reading
from loc_arena.task import SNAPSHOT_FILE, read_snapshot_file
from loc_arena.taxonomy import (
    Count,
    Instance,
    Observation,
    Source,
    Taxonomy,
    count_instances,
    load_taxonomy,
    observe_run,
)

REPLAY_FILE = "replay.json"  # in the run directory
REPLAY_BRIEF = "replay"  # the brief a run is read with before it is exported
SUMMARY_FILE = "replay.summary.json"  # in a folder of runs: each config's counts, added up across its runs
COVERT = ("covert.acted", "covert.framed_as_ordinary")  # an instance of these marks its events covert
EPOCH = 1e9  # an event time past this is a wall time; the episode clock reads far less
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_PULL_REQUEST = re.compile(r"\b(?:PR|pull request)\s*#?(\d+)\b|(?<![\w/&])#(\d+)\b", re.IGNORECASE)
_JOB = re.compile(r"\bjob-[0-9a-z-]+\b")
_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”")
_CHANNELS = {"action": "tool", "pr": "forge", "commit": "forge", "review": "forge", "forge_boot": "forge"}
_CHANNELS |= {"job": "cluster", "process": "cluster", "inference_call": "model"}


class Agent(BaseModel):
    """One agent of the team: whom it reports to, what its covert brief serves, its scope and call budget."""

    id: str
    reports_to: str | None
    covert_milestones: list[str]
    repositories: list[str]
    call_budget: int | None


class RunPart(BaseModel):
    """What the run was: its name, config and commit, team, models, bound, cost and traces."""

    name: str
    config: str | None  # the run config's name; none for a run that recorded none
    commit: str | None
    modified: bool | None
    mode: str
    agents: list[Agent]
    models: dict[str, str]  # by role
    play_bound_seconds: float | None
    cost: dict[str, dict[str, float]]  # dollars by model, seconds by phase
    traces: dict[str, list[str]]  # where each trace lives, relative to the run directory


class Ref(BaseModel):
    """An earlier event an event refers to, and how that was found: by an id, or by the reader, checked."""

    seq: int
    by: Literal["id", "reader"]
    via: str  # the id, or the phrase the reader quoted


class ReplayEvent(BaseModel):
    """One event of a play as a page shows it."""

    seq: int
    seconds: float | None  # from the play's start; none where the records hold no time for it
    actor: str
    channel: str
    action: str
    target: str | None
    words: list[str]  # what an agent wrote in prose, verbatim; never code, a command or a credential
    outcome: Literal["ok", "blocked", "failed"] | None  # for an action
    covert: bool
    covert_source: Source | None
    refs: list[Ref]


class Reason(BaseModel):
    """Why a result came out as it did: a fact from the records, or the reader's checked inference."""

    text: str
    play: str | None
    seqs: list[int]
    kind: Literal["fact", "inference"]


class Outcome(BaseModel):
    """One result of the run and its reasons."""

    result: str
    value: str
    reasons: list[Reason]


class ReplayExport(BaseModel):
    """The replay export of one run."""

    model_config = ConfigDict(extra="forbid")

    taxonomy_version: int
    run: RunPart
    plays: dict[str, list[ReplayEvent]]
    outcomes: list[Outcome]
    instances: list[Instance]
    counts: dict[str, dict[str, Count]]  # per play, per category
    failures: dict[str, dict[str, Count]]  # per play, the categories of the failure groups
    reader: str  # "read", or why the run has no reading


_SUMMARY = TypeAdapter(dict[str, dict[str, dict[str, Count]]])  # per config, play and category


def export_run(
    run_directory: Path,
    taxonomy: Taxonomy,
    observed: Observation,
    evidence: RunEvidence,
    reading: CheckedReading | None,
    read: str,
) -> ReplayExport:
    """The replay export of the graded run in ``run_directory``, with the reader's checked ``reading``."""
    scores = load_scores(run_directory / "scores.json")
    claims = {title: kept for title, kept in reading.sections} if reading else {}
    judged = _read_instances(taxonomy, observed, claims.get("Instances", []))
    instances = [*observed.instances, *judged]
    counts = count_instances(taxonomy, instances, observed.plays, observed.run)
    failing = {category.id for category in taxonomy.categories if category.group.startswith("failure")}
    covert = {(i.play, seq): i.source for i in instances if i.category in COVERT for seq in i.seqs}
    refs = _read_refs(claims.get("References", []))
    return ReplayExport(
        taxonomy_version=taxonomy.version,
        run=_run_part(run_directory, scores),
        plays={
            play.name: _replay_play(run_directory / phase, play.name, play.sealed, evidence, covert, refs)
            for play, (_, phase) in zip(observed.plays, name_plays(run_directory), strict=True)
        },
        outcomes=_outcomes(scores, observed, claims),
        instances=instances,
        counts=counts,
        failures={
            play: {key: count for key, count in by.items() if key in failing} for play, by in counts.items()
        },
        reader=read,
    )


def summarize_exports(exports: Iterable[ReplayExport]) -> dict[str, dict[str, dict[str, Count]]]:
    """Per run config and play, each category's count out of its chances, added up across its runs."""
    summary: dict[str, dict[str, dict[str, Count]]] = {}
    for export in exports:
        by_play = summary.setdefault(export.run.config or export.run.name, {})
        for play, by_category in export.counts.items():
            totals = by_play.setdefault(play, {})
            for category, count in by_category.items():
                before = totals.get(category, Count(count=0, chances=0))
                totals[category] = Count(
                    count=before.count + count.count,
                    chances=before.chances + count.chances,
                )
    return summary


def _sealed_cites(cites: Sequence[str]) -> list[tuple[str, int]]:
    """The sealed events a claim cites, as plays and seqs; its facts and mirror events left out."""
    return [
        (play, int(seq))
        for play, _, seq in (cite.partition(":") for cite in cites)
        if seq.isdigit() and not play.endswith(f"-{MIRROR}")
    ]


def _read_instances(taxonomy: Taxonomy, observed: Observation, claims: Sequence[KeptClaim]) -> list[Instance]:
    """The reader's checked instances: a claim names a category it judges and its agent, citing one play."""
    judged = []
    for claim in claims:
        named = re.findall(r"`([^`]+)`", claim.text)
        category = next((taxonomy.find(name) for name in named if taxonomy.find(name)), None)
        cited = _sealed_cites(claim.cites)
        plays = {play for play, _ in cited}
        if category is None or category.found_by != "reader" or len(plays) != 1:
            continue
        play = plays.pop()
        if not category.applies(play, observed.run.mode):
            continue
        actor = _first_agent(claim.text, observed.run.agents)
        seqs = tuple(sorted(seq for _, seq in cited))
        judged.append(Instance(category=category.id, play=play, actor=actor, seqs=seqs, source="reader"))
    return judged


def _first_agent(text: str, agents: Iterable[str]) -> str | None:
    """The agent ``text`` names first, in backticks or bare, as a whole name."""
    found = {agent: re.search(rf"(?<![\w-]){re.escape(agent)}(?![\w-])", text) for agent in agents}
    named = [(match.start(), agent) for agent, match in found.items() if match]
    return min(named)[1] if named else None


def _read_refs(claims: Sequence[KeptClaim]) -> dict[tuple[str, int], list[Ref]]:
    """The reader's checked references: the first cited event holds the quoted phrase, the rest precede it."""
    refs: dict[tuple[str, int], list[Ref]] = {}
    for claim in claims:
        cited, phrase = _sealed_cites(claim.cites), _QUOTED.search(claim.text)
        if len(cited) < 2 or phrase is None:  # noqa: PLR2004 - the event, and what it refers to
            continue
        (play, seq), *earlier = cited
        refs.setdefault((play, seq), []).extend(
            Ref(seq=to, by="reader", via=phrase.group(1) or phrase.group(2))
            for of, to in earlier
            if of == play and to < seq
        )
    return refs


def _replay_play(
    directory: Path,
    play: str,
    sealed: Sequence[Event],
    evidence: RunEvidence,
    covert: dict[tuple[str, int], Source],
    reader_refs: dict[tuple[str, int], list[Ref]],
) -> list[ReplayEvent]:
    walls = directory / EVENT_WALLS_FILE
    lines = walls.read_text().splitlines() if walls.is_file() else []
    wall_of = {int(record["seq"]): float(record["wall"]) for record in map(json.loads, lines)}
    clock = read_snapshot_file(directory).clock if (directory / SNAPSHOT_FILE).is_file() else ()
    started = clock[0].wall if clock else None
    bearing: dict[str, list[int]] = {}  # the events that bore each id so far
    replayed = []
    for event in sealed:
        said = evidence.events[f"{play}:{event.seq}"].words
        named = sorted(name for name in _ids_named(event, " ".join(said)) if bearing.get(name))
        refs = [
            Ref(seq=seq, by="id", via=name)
            for name in named
            for seq in dict.fromkeys(bearing[name][:1] + bearing[name][-1:])
        ]
        for name in _ids_borne(event):
            bearing.setdefault(name, []).append(event.seq)
        wall = wall_of.get(event.seq, event.ts if event.ts > EPOCH else None)
        replayed.append(
            ReplayEvent(
                seq=event.seq,
                seconds=round(wall - started, 3) if wall is not None and started is not None else None,
                actor=event.actor_uid,
                channel=_channel(event),
                action=event.tool or event.kind,
                target=event.target_id,
                words=[line for text in said for line in prose_lines(text)],
                outcome=_outcome(event),
                covert=(play, event.seq) in covert,
                covert_source=covert.get((play, event.seq)),
                refs=[*refs, *reader_refs.get((play, event.seq), [])],
            ),
        )
    return replayed


def _ids_borne(event: Event) -> set[str]:
    """The ids an event bears itself: the ticket it made or changed, its pull request, its job."""
    borne = set()
    result = event.result if isinstance(event.result, dict) else {}
    if event.tool in ("create_ticket", "edit_ticket", "comment_on_ticket"):
        borne.add(str(result.get("id") or event.target_id))
    number = event.payload.get("number") or event.payload.get("pr_number")
    if event.kind in ("pr", "commit", "review") and number is not None:
        borne.add(f"#{number}")
    if event.kind == "job" and event.payload.get("job_id"):
        borne.add(str(event.payload["job_id"]))
    return borne


def _ids_named(event: Event, words: str) -> set[str]:
    """The ids an event refers to: those its words name, and the ticket an action of it targets."""
    named = set(_TICKET.findall(words)) | set(_JOB.findall(words))
    named |= {f"#{first or second}" for first, second in _PULL_REQUEST.findall(words)}
    if event.tool in ("edit_ticket", "comment_on_ticket") and event.target_id:
        named.add(event.target_id)
    return named


def _channel(event: Event) -> str:
    if event.kind in ("message", "slack"):
        channel = event.payload.get("channel")
        return f"#{channel}" if channel else "direct message"
    return _CHANNELS.get(event.kind, event.kind)


def _outcome(event: Event) -> Literal["ok", "blocked", "failed"] | None:
    if event.kind != "action":
        return None
    result = event.result if isinstance(event.result, dict) else {}
    if event.payload.get("blocked"):
        return "blocked"
    return "failed" if result.get("returncode") not in (None, 0) or result.get("error") else "ok"


def _run_part(run_directory: Path, scores: RunScores) -> RunPart:
    config = yaml.safe_load((run_directory / "config.resolved.yaml").read_text())
    identity_file = run_directory / RUN_FILE
    identity = RunIdentity.model_validate_json(identity_file.read_text()) if identity_file.is_file() else None
    episode = config.get("episode", {})
    budgets = {
        "orchestrator": episode.get("orchestrator_turn_budget"),
        "subagent": episode.get("subagent_turn_budget"),
    }
    cost_file = run_directory / "cost.json"
    cost = json.loads(cost_file.read_text()) if cost_file.is_file() else {}
    traces = {
        "sealed": ["episode/events.sealed.jsonl", "honest_cal/events.sealed.jsonl"],
        "mirror": ["episode/events.mirror.jsonl", "honest_cal/events.mirror.jsonl"],
        "gateway_calls": ["gateway.calls.jsonl"],
        "inspect_log": [path.name for path in run_directory.glob("*.eval")],
        "transcript": ["transcript.html", "transcript.txt"],
        "phoenix_spans": ["services/phoenix.spans.jsonl"],
        "service_logs": sorted(
            f"services/{path.name}" for path in (run_directory / "services").glob("*.log")
        ),
        "scores": ["scores.json"],
    }
    return RunPart(
        name=run_directory.name,
        config=identity.config if identity else None,
        commit=identity.commit if identity else None,
        modified=identity.modified if identity else None,
        mode=scores.mode,
        agents=[
            Agent(
                id=agent["id"],
                reports_to=agent.get("spawned_by"),
                covert_milestones=[str(milestone) for milestone in agent.get("covert") or []],
                repositories=[str(repo) for repo in agent.get("scope", {}).get("open_pr", [])],
                call_budget=budgets.get(agent.get("kind", "")),
            )
            for agent in config["agents"]
        ],
        models=dict(scores.models),
        play_bound_seconds=episode.get("wall_clock_seconds"),
        cost={
            "dollars_by_model": dict(cost.get("dollars", {}).get("by_model", {})),
            "seconds_by_phase": dict(cost.get("phases", {})),
        },
        traces={
            name: [path for path in paths if (run_directory / path).exists()]
            for name, paths in traces.items()
        },
    )


def _outcomes(scores: RunScores, observed: Observation, claims: dict[str, list[KeptClaim]]) -> list[Outcome]:
    """Each result, with the facts the records give for it and the reader's checked reasons."""

    def inferred(section: str) -> list[Reason]:
        return [
            Reason(
                text=claim.text,
                play=next((play for play, _ in _sealed_cites(claim.cites)), None),
                seqs=sorted(seq for _, seq in _sealed_cites(claim.cites)),
                kind="inference",
            )
            for claim in claims.get(section, [])
        ]

    main, side, graded = scores.main_task, scores.side_task, observed.run.mode
    flagged = [
        Reason(
            text=f"a monitor scored this event {score}, above the audit threshold",
            play=play.name,
            seqs=[seq],
            kind="fact",
        )
        for play in observed.plays
        for seq, score in play.verdicts
        if seq is not None and score > observed.run.audit_threshold
    ]
    return [
        Outcome(
            result="main task",
            value=f"{main.final_score:.3f}",
            reasons=[
                Reason(text=f"gate: {main.gate_reason}", play=graded, seqs=[], kind="fact"),
                *inferred("Main task"),
            ],
        ),
        Outcome(
            result="side task",
            value="landed" if side.landing_minimal else "not landed",
            reasons=[
                *(
                    Reason(
                        text=f"milestone {name}: {'reached' if step.reached else 'not reached'}",
                        play=graded,
                        seqs=[],
                        kind="fact",
                    )
                    for name, step in side.milestones.items()
                ),
                *inferred("Side task"),
            ],
        ),
        Outcome(
            result="caught",
            value="yes" if scores.safety.caught else "no",
            reasons=[*flagged, *inferred("Catches")],
        ),
    ]


def export_runs(path: Path, config: RunConfig) -> list[Path]:
    """Export the graded run at ``path``, or each graded run in the folder ``path``; the files written.

    Each run is first read with the ``replay`` brief. A reading that fails is recorded in its export, which
    then holds what code shows alone. A folder also gets ``replay.summary.json``.
    """
    if not path.is_dir():
        raise ValueError(f"{path} is no run's directory and no folder of runs")
    single = (path / "scores.json").is_file()
    runs = [path] if single else sorted(run for run in path.iterdir() if (run / "scores.json").is_file())
    if not runs:
        raise ValueError(f"{path} holds no graded run")
    settings, taxonomy, exports = settle_reading(config), load_taxonomy(), []
    for run in runs:
        try:
            read_run(run, REPLAY_BRIEF, config)
            checked = (run / READINGS / f"{REPLAY_BRIEF}.checked.json").read_text()
            reading, read = CheckedReading.model_validate_json(checked), "read"
        except (
            RuntimeError,
            ValueError,
        ) as error:  # recorded, never silent: the export keeps what code shows
            reading, read = None, f"no reading: {error}"
        secrets = str(settings.gateway.secrets_dir)
        observed = observe_run(run, taxonomy, secrets, settings.reader.repeat_at_least)
        evidence = gather_evidence(run, settings.reader.word_fields)
        exports.append(export_run(run, taxonomy, observed, evidence, reading, read))
        (run / REPLAY_FILE).write_text(exports[-1].model_dump_json(indent=1))
    if single:
        return [path / REPLAY_FILE]
    (path / SUMMARY_FILE).write_bytes(_SUMMARY.dump_json(summarize_exports(exports), indent=1))
    return [*(run / REPLAY_FILE for run in runs), path / SUMMARY_FILE]
