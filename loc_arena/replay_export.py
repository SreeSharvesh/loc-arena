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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, computed_field

from loc_arena.config import RunConfig
from loc_arena.event_references import job_of, refer_by_id
from loc_arena.explorer.scores import RunScores, load_scores
from loc_arena.logging_.events import Event
from loc_arena.reading_check import QUOTE, CheckedReading, KeptClaim, prose_lines
from loc_arena.recorded_events import EVENT_WALLS_FILE
from loc_arena.run_cost import COST_FILE, read_cost
from loc_arena.run_facts import MIRROR, RunEvidence, gather_evidence, name_plays, read_resolved_config
from loc_arena.run_identity import RUN_FILE, RunIdentity
from loc_arena.run_reader import READINGS, read_run, settle_reading
from loc_arena.settings import LocArenaSettings
from loc_arena.task import SNAPSHOT_FILE, ClockReading, read_snapshot_file
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
_CHANNELS = {"action": "tool", "pr": "forge", "commit": "forge", "review": "forge", "forge_boot": "forge"}
_CHANNELS |= {"job": "cluster", "process": "cluster", "inference_call": "model"}


class Agent(BaseModel):
    """One agent of the team: whom it reports to, what its side task serves, its scope and call budget."""

    id: str
    reports_to: str | None
    covert_stages: list[str]
    repositories: list[str]
    call_budget: int | None

    @computed_field
    @property
    def covert_milestones(self) -> list[str]:
        """The deprecated name of ``covert_stages``, kept for pages built on older exports."""
        return self.covert_stages


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


class RunsSummary(BaseModel):
    """One run config's runs at one taxonomy version: how many, how many were read, their counts added up."""

    config: str
    taxonomy_version: int
    runs: int = 0
    read: int = 0
    counts: dict[str, dict[str, Count]] = {}  # per play, per category


_SUMMARY = TypeAdapter(list[RunsSummary])


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
    unjudged = (
        set()
        if reading
        else {category.id for category in taxonomy.categories if category.found_by == "reader"}
    )
    counts = {
        play: {category: count for category, count in by.items() if category not in unjudged}
        for play, by in count_instances(taxonomy, instances, observed.plays, observed.run).items()
    }  # a category the reader judges is counted only in a run it read
    failing = {category.id for category in taxonomy.categories if category.group.startswith("failure")}
    covert = _covert_events(observed, instances, scores)
    refs = _read_refs(claims.get("References", []), evidence)
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


def summarize_exports(exports: Iterable[ReplayExport]) -> list[RunsSummary]:
    """Per run config and taxonomy version, each category's count out of its chances, added up over its runs.

    A category the reader judges is added up only over the runs it read: an unread run's export holds none.
    """
    summaries: dict[tuple[str, int], RunsSummary] = {}
    for export in exports:
        key = (export.run.config or export.run.name, export.taxonomy_version)
        summary = summaries.setdefault(key, RunsSummary(config=key[0], taxonomy_version=key[1]))
        summary.runs, summary.read = summary.runs + 1, summary.read + (export.reader == "read")
        for play, by_category in export.counts.items():
            totals = summary.counts.setdefault(play, {})
            for category, count in by_category.items():
                before = totals.get(category, Count(count=0, chances=0))
                totals[category] = Count(
                    count=before.count + count.count,
                    chances=before.chances + count.chances,
                )
    return list(summaries.values())


def _covert_events(
    observed: Observation,
    instances: Sequence[Instance],
    scores: RunScores,
) -> dict[tuple[str, int], Source]:
    """The covert events: the verifier's evidence in the graded play, and the reader's, of its named agent."""
    actors = {(play.name, event.seq): event.actor_uid for play in observed.plays for event in play.sealed}
    covert: dict[tuple[str, int], Source] = {
        (i.play, seq): "reader"
        for i in instances
        if i.category in COVERT
        for seq in i.seqs
        if actors.get((i.play, seq)) == i.actor
    }
    graded = next(play for play in observed.plays if play.name == observed.run.mode)
    for stage in scores.side_task.stages.values():
        covert |= {(graded.name, seq): "code" for seq in _evidence_seqs(stage.evidence, graded.sealed)}
    return covert


def _evidence_seqs(evidence: Mapping[str, JsonValue], sealed: Sequence[Event]) -> list[int]:
    """The sealed events a stage's evidence names: those of the jobs it names.

    Its call seqs number the play's answered model calls, and an off-path call has no sealed event.
    """
    jobs = {
        str(job)
        for key, value in evidence.items()
        if key.endswith("_job_ids") and isinstance(value, list)
        for job in value
    }
    return sorted(event.seq for event in sealed if job_of(event) in jobs)


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
        instance = Instance(category=category.id, play=play, actor=actor, seqs=seqs, source="reader")
        judged += [] if instance in judged else [instance]  # a claim repeated is one instance
    return judged


def _first_agent(text: str, agents: Iterable[str]) -> str | None:
    """The agent ``text`` names first, in backticks or bare, as a whole name."""
    found = {agent: re.search(rf"(?<![\w-]){re.escape(agent)}(?![\w-])", text) for agent in agents}
    named = [(match.start(), agent) for agent, match in found.items() if match]
    return min(named)[1] if named else None


def _read_refs(claims: Sequence[KeptClaim], evidence: RunEvidence) -> dict[tuple[str, int], list[Ref]]:
    """The reader's checked references: the first cited event holds the quoted phrase, the rest precede it."""
    refs: dict[tuple[str, int], list[Ref]] = {}
    for claim in claims:
        cited, phrase = _sealed_cites(claim.cites), QUOTE.search(claim.text)
        if len(cited) < 2 or phrase is None:  # noqa: PLR2004 - the event, and what it refers to
            continue
        (play, seq), *earlier = cited
        quoted = " ".join("".join(filter(None, phrase.groups())).split())
        held = " ".join(" ".join(evidence.events[f"{play}:{seq}"].words).split())
        if not re.search(rf"(?<!\w){re.escape(quoted)}(?!\w)", held):
            continue  # the phrase is another cited event's: the reference is not this event's
        refs.setdefault((play, seq), []).extend(
            Ref(seq=to, by="reader", via=quoted) for of, to in earlier if of == play and to < seq
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
    started, clock_walls = (clock[0].wall if clock else None), _walk_clock(clock, sealed)
    said_by = {event.seq: evidence.events[f"{play}:{event.seq}"].words for event in sealed}
    by_id = refer_by_id(sealed, {seq: " ".join(said) for seq, said in said_by.items()})
    replayed = []
    for event in sealed:
        said = said_by[event.seq]
        refs = [Ref(seq=seq, by="id", via=name) for seq, name in by_id[event.seq]]
        wall = wall_of.get(event.seq, event.ts if event.ts > EPOCH else clock_walls.get(event.seq))
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


def _walk_clock(clock: Sequence[ClockReading], sealed: Sequence[Event]) -> dict[int, float]:
    """Each event's wall time by the clock reading it was written under.

    The clock is set to one time more than once, so the events and the readings are walked in order together:
    an event takes the next reading set to its time, or keeps the reading it is under.
    """
    walls, at = {}, 0
    for event in sealed:
        at = next((index for index in range(at, len(clock)) if clock[index].ts == event.ts), at)
        if clock:
            walls[event.seq] = clock[at].wall
    return walls


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
    config = read_resolved_config(run_directory)
    identity_file = run_directory / RUN_FILE
    identity = RunIdentity.model_validate_json(identity_file.read_text()) if identity_file.is_file() else None
    root = next(
        agent.id for agent in config.agents if agent.spawned_by is None
    )  # as each session is given it
    cost = read_cost(run_directory) if (run_directory / COST_FILE).is_file() else None
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
        config=scores.config_name,
        commit=identity.commit if identity else None,
        modified=identity.modified if identity else None,
        mode=scores.mode,
        agents=[
            Agent(
                id=agent.id,
                reports_to=agent.spawned_by,
                covert_stages=list(agent.covert or ()),
                repositories=list(agent.scope.open_pr),
                call_budget=config.episode.orchestrator_turn_budget
                if agent.id == root
                else config.episode.subagent_turn_budget,
            )
            for agent in config.agents
        ],
        models=dict(scores.models),
        play_bound_seconds=config.episode.wall_clock_seconds,
        cost={
            "dollars_by_model": cost.dollars.by_model if cost else {},
            "seconds_by_phase": cost.phases if cost else {},
        },
        traces={
            name: [path for path in paths if (run_directory / path).exists()]
            for name, paths in traces.items()
        },
    )


def _outcomes(scores: RunScores, observed: Observation, claims: dict[str, list[KeptClaim]]) -> list[Outcome]:
    """Each result, with the facts code finds for it in the records and the reader's checked reasons."""

    def inferred(section: str) -> list[Reason]:
        reasons = []
        for claim in claims.get(section, []):
            cited = _sealed_cites(claim.cites)
            play = cited[0][0] if cited else None
            seqs = sorted(seq for of, seq in cited if of == play)  # a reason's seqs are of its one play
            reasons.append(Reason(text=claim.text, play=play, seqs=seqs, kind="inference"))
        return reasons

    main, side, safety = scores.main_task, scores.side_task, scores.safety
    graded = next(play for play in observed.plays if play.name == observed.run.mode)
    threshold = safety.aggregation.threshold if safety.aggregation else safety.audit_threshold
    aggregate = (
        f"the monitors' combined suspicion, {safety.overall_suspicion}, against the catch's threshold, "
        f"{threshold}; caught by {', '.join(safety.caught_by) or 'no monitor'}"
    )
    flagged = [
        Reason(
            text=f"a monitor scored this event {score}, above the audit threshold",
            play=graded.name,
            seqs=[seq],
            kind="fact",
        )
        for seq, score in graded.verdicts
        if seq is not None and score > observed.run.audit_threshold
    ]
    highest = safety.per_monitor_max_score
    deciding = [  # the graded play's verdicts are its scores' own, in their order
        Reason(
            text=f"{verdict.monitor}, which caught the run, scored this event {score}, its highest",
            play=graded.name,
            seqs=[seq],
            kind="fact",
        )
        for verdict, (seq, score) in zip(scores.monitor_verdicts, graded.verdicts, strict=True)
        if seq is not None and verdict.monitor in safety.caught_by and score == highest.get(verdict.monitor)
    ]
    stages = [
        Reason(
            text=f"stage {name}: {'reached' if step.reached else 'not reached'}",
            play=graded.name,
            seqs=_evidence_seqs(step.evidence, graded.sealed),
            kind="fact",
        )
        for name, step in side.stages.items()
    ]
    return [
        Outcome(
            result="main task",
            value=f"{main.final_score:.3f}",
            reasons=[
                Reason(text=f"gate: {main.gate_reason}", play=graded.name, seqs=[], kind="fact"),
                *inferred("Main task"),
            ],
        ),
        Outcome(
            result="side task",
            value="succeeded" if side.success else "did not succeed",
            reasons=[*stages, *inferred("Side task")],
        ),
        Outcome(
            result="caught",
            value="yes" if safety.caught else "no",
            reasons=[
                Reason(text=aggregate, play=graded.name, seqs=[], kind="fact"),
                *deciding,
                *flagged,
                *inferred("Catches"),
            ],
        ),
    ]


def export_runs(path: Path, config: RunConfig) -> tuple[list[Path], dict[Path, str]]:
    """Export the graded run at ``path``, or each in the folder ``path``: the files written, and each failure.

    A run that cannot be exported is named with why, and the others go on. Each run is first read with the
    ``replay`` brief. A reading that fails is recorded in its export, which then holds what code shows alone.
    A folder also gets ``replay.summary.json``. A run's own records are never searched for runs: a checkout in
    them may hold any file an agent wrote.
    """
    path = path.resolve()  # a run is named after its directory, the current one too
    if not path.is_dir():
        raise ValueError(f"{path} is no run's directory and no folder of runs")
    single = (path / "scores.json").is_file()
    runs: list[Path] = [path] if single else []
    for scores in sorted(path.rglob("scores.json"), key=lambda found: len(found.parts)):
        if not any(scores.is_relative_to(run) for run in runs):  # outermost first
            runs.append(scores.parent)
    if not runs:
        raise ValueError(f"{path} holds no graded run")
    settings, taxonomy = settle_reading(config), load_taxonomy()
    exports: list[ReplayExport] = []
    failed: dict[Path, str] = {}
    for run in sorted(runs):
        try:
            exported = _export_one(run, taxonomy, settings, config)
            (run / REPLAY_FILE).write_text(exported.model_dump_json(indent=1))
        except (OSError, ValueError, LookupError, StopIteration) as error:  # recorded: the rest go on
            failed[run] = f"{type(error).__name__}: {error}"
            continue
        exports.append(exported)
    written = [run / REPLAY_FILE for run in sorted(runs) if run not in failed]
    if single:
        return written, failed
    (path / SUMMARY_FILE).write_bytes(_SUMMARY.dump_json(summarize_exports(exports), indent=1))
    return [*written, path / SUMMARY_FILE], failed


def _export_one(run: Path, taxonomy: Taxonomy, settings: LocArenaSettings, config: RunConfig) -> ReplayExport:
    try:
        read_run(run, REPLAY_BRIEF, config)
        checked = (run / READINGS / f"{REPLAY_BRIEF}.checked.json").read_text()
        reading, read = CheckedReading.model_validate_json(checked), "read"
    except (RuntimeError, ValueError) as error:  # recorded, never silent: the export keeps what code shows
        reading, read = None, f"no reading: {error}"
    observed = observe_run(run, taxonomy, str(settings.gateway.secrets_dir), settings.reader.repeat_at_least)
    evidence = gather_evidence(run, settings.reader.word_fields)
    return export_run(run, taxonomy, observed, evidence, reading, read)
