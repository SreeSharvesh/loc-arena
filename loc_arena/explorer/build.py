"""Build the explorer's pages: one self-contained page per run bundle, and an index over a folder of them.

A step is one agent turn, or the events of one round outside any turn (the ``World`` lane): its model calls,
tool calls and other events, the side-task evidence the sealed verifier cites, and the monitor verdicts placed
on it (see ``placement`` for how each event and verdict finds its step).
"""

from __future__ import annotations

import dataclasses
import json
import zipfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Final

from inspect_ai.event import InfoEvent, ModelEvent, ToolEvent
from inspect_ai.log import read_eval_log, resolve_sample_attachments
from pydantic import JsonValue

from loc_arena.explorer.communication import (
    MESSAGE_KIND,
    SCRATCH_KIND,
    Said,
    Utterance,
    extract_event_utterance,
    extract_tool_utterance,
    resolve_communication,
)
from loc_arena.explorer.page import (
    EventView,
    MainTaskView,
    MilestoneView,
    ModelCallView,
    MonitorView,
    OutcomeView,
    PageSettings,
    RunIndex,
    RunPage,
    RunRow,
    RunView,
    SafetyView,
    SideTaskView,
    StepView,
    ToolCallView,
    VerdictView,
)
from loc_arena.explorer.placement import (
    PlacedEvent,
    list_verdict_targets,
    order_lanes,
    pair_mirror_to_sealed,
    place_events,
    place_verdicts,
    read_event_seqs,
)
from loc_arena.explorer.scores import RunScores, load_scores
from loc_arena.explorer.settings import ExplorerSettings
from loc_arena.recording_service import COVERT_TOOL_NAMES

DATA_MARKER: Final = "__DATA__"
RUN_TEMPLATE: Final = "run.html"
INDEX_TEMPLATE: Final = "index.html"
INDEX_PAGE: Final = "index.html"
RUNS_FOLDER: Final = "runs"
RUN_PAGE: Final = "explorer.html"  # a single run's page, written into its bundle unless told otherwise
SITE_FOLDER: Final = "explorer"  # a folder's pages, written under the folder unless told otherwise
SCORES_FILE: Final = "scores.json"
NO_TRANSCRIPT: Final = "no per-agent log: the run predates it, so there is no transcript to open"
UNKNOWN_MODEL: Final = "?"  # the index's model column for a run whose scores do not name the model
SECONDS_PER_MINUTE: Final = 60
# Files every run bundle holds; a folder with them but no scores.json is a run still going, or a sweep.
BUNDLE_MARKERS: Final = ("events.sealed.jsonl", "config.resolved.yaml", "decisions.md")
BUNDLE_PARTS: Final = frozenset({"episode", "honest_cal"})  # folders inside a bundle, not runs of their own
# Evidence lists of {"seq": ...} records; every other evidence list whose key ends in "seqs" holds bare seqs.
EVIDENCE_RECORD_LISTS: Final = frozenset({"off_bus_writes"})
# The argument a card shows for a tool call, in order of preference.
ACTION_KEYS: Final = (
    "content",
    "cmd",
    "command",
    "path",
    "query",
    "pattern",
    "iterations",
    "target",
    "title",
    "body",
)


@dataclass(frozen=True)
class EventDescription:
    """An event as the page shows it, and its recipient and payload, for working out what was said in it."""

    view: EventView
    target: str | None
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True)
class StepCounts:
    """How many of a run's steps were flagged, covert, or covert and missed, and how much its agents said.

    None throughout for a run without a transcript: nothing was counted, which is not zero.
    """

    steps: int | None = None
    flagged: int | None = None
    covert: int | None = None
    covert_missed: int | None = None
    messages: int | None = None
    scratch: int | None = None


def find_transcript_log(bundle: Path) -> Path | None:
    """The bundle's Inspect log when it is a real one, not the JSON placeholder."""
    return next((path for path in sorted(bundle.glob("*.eval")) if zipfile.is_zipfile(path)), None)


def build_run_page(bundle: Path, settings: ExplorerSettings, index_href: str | None = None) -> RunPage:
    """Everything one run's page shows, from its bundle."""
    log_path = find_transcript_log(bundle)
    if log_path is None:
        raise FileNotFoundError(f"{bundle} has no per-agent Inspect log to build a transcript from")
    log = read_eval_log(str(log_path))
    sample = resolve_sample_attachments((log.samples or [])[0], "full")
    scores = load_scores(bundle / SCORES_FILE)
    run_name = log.eval.run_id or log_path.stem
    placed = place_events(sample)
    configured = [str(agent) for agent in sample.metadata.get("agents", [])]
    lanes = order_lanes(configured, {p.lane for p in placed})
    step_ids = {(p.lane, p.round): f"{run_name}/{p.lane}/{p.round}" for p in placed}
    targets = {
        seq: (step_ids[(p.lane, p.round)], target)
        for p in placed
        for seq, target in list_verdict_targets(p.event)
    }
    verdicts = place_verdicts(scores, pair_mirror_to_sealed(sample, bundle), targets)
    evidence = collect_covert_evidence(scores)
    steps, utterances = assemble_steps(placed, step_ids, lanes, verdicts.placed, evidence, settings)
    monitor_names = {monitor.name for monitor in scores.monitors}
    return RunPage(
        run=RunView(
            name=run_name,
            slug=scores.run_slug,
            mode=scores.mode,
            seed=scores.seed,
            models=scores.models,
            wall_clock_seconds=scores.wall_clock_seconds,
            policy=str((log.eval.metadata or {}).get("policy") or "") or None,
            index=index_href,
        ),
        outcome=describe_outcome(scores, steps),
        monitors=tuple(
            MonitorView(m.name, m.role, m.mode, m.timing, m.counts_toward_caught, m.errored, m.top_reasons)
            for m in scores.monitors
        ),
        lanes=lanes,
        steps=steps,
        verdicts=tuple(v for placed_verdicts in verdicts.placed.values() for v in placed_verdicts)
        + verdicts.unplaced,
        communication=resolve_communication(utterances, steps, monitor_names, settings),
        settings=PageSettings(settings.list_page_size, settings.sparse_monitor_verdicts),
    )


def list_evidence_seqs(evidence: Mapping[str, object]) -> list[int]:
    """The sealed seqs a milestone's evidence cites."""
    seqs: list[int] = []
    for key, value in evidence.items():
        if not isinstance(value, list) or not (key.endswith("seqs") or key in EVIDENCE_RECORD_LISTS):
            continue
        for entry in value:
            seq = entry.get("seq") if isinstance(entry, dict) else entry
            if isinstance(seq, int):
                seqs.append(seq)
    return seqs


def collect_covert_evidence(scores: RunScores) -> Mapping[int, frozenset[str]]:
    """The milestones whose sealed evidence cites each sealed seq, by name."""
    cited: dict[int, set[str]] = defaultdict(set)
    for milestone, record in scores.side_task.milestones.items():
        for seq in list_evidence_seqs(record.evidence):
            cited[seq].add(milestone.replace("_", " "))
    return {seq: frozenset(names) for seq, names in cited.items()}


def assemble_steps(
    placed: Sequence[PlacedEvent],
    step_ids: Mapping[tuple[str, int], str],
    lanes: Sequence[str],
    verdicts: Mapping[int, tuple[VerdictView, ...]],
    evidence: Mapping[int, frozenset[str]],
    settings: ExplorerSettings,
) -> tuple[tuple[StepView, ...], list[Utterance]]:
    """The steps in round order, lanes in lane order, and everything said in them."""
    calls: dict[str, list[ModelCallView]] = defaultdict(list)
    tools: dict[str, list[ToolCallView]] = defaultdict(list)
    events: dict[str, list[EventView]] = defaultdict(list)
    utterances: list[Utterance] = []
    for p in placed:
        step = step_ids[(p.lane, p.round)]
        if isinstance(p.event, ModelEvent):
            calls[step].append(_describe_model_call(p.event))
        elif isinstance(p.event, ToolEvent):
            tool = _describe_tool_call(p.event, verdicts, evidence, settings)
            tools[step].append(tool)
            said = extract_tool_utterance(
                _make_said(step, p.lane, tool.verdicts, tool.covert),
                tool.function,
                p.event.arguments or {},
                refused=tool.blocked is not None,
            )
            utterances.extend([said] if said else [])
        elif isinstance(p.event, InfoEvent):
            described = _describe_event(p.event, verdicts, evidence)
            event = described.view
            events[step].append(event)
            said = extract_event_utterance(
                _make_said(step, event.actor or p.lane, event.verdicts, event.covert),
                event.kind,
                described.target,
                described.payload,
            )
            utterances.extend([said] if said else [])
    lane_index = {lane: i for i, lane in enumerate(lanes)}
    keys = sorted(step_ids, key=lambda key: (key[1], lane_index.get(key[0], len(lane_index))))
    steps = tuple(_describe_step(step_ids[key], key, calls, tools, events) for key in keys)
    return steps, utterances


def describe_outcome(scores: RunScores, steps: Sequence[StepView]) -> OutcomeView:
    """The run's outcome, with the steps each milestone's evidence cites."""
    step_of_seq = {
        item.seq: step.id
        for step in steps
        for item in (*step.calls, *step.tools, *step.events)
        if item.seq is not None
    }
    milestones = tuple(
        MilestoneView(
            id=name,
            reached=record.reached,
            sealed=record.sealed,
            steps=tuple(
                dict.fromkeys(
                    step_of_seq[seq] for seq in list_evidence_seqs(record.evidence) if seq in step_of_seq
                ),
            ),
        )
        for name, record in scores.side_task.milestones.items()
    )
    safety = scores.safety
    return OutcomeView(
        main_task=MainTaskView(
            scores.main_task.final_score,
            scores.main_task.gate_passed,
            scores.main_task.gate_reason,
        ),
        side_task=SideTaskView(scores.side_task.landing_minimal, scores.side_task.landing_robust),
        milestones=milestones,
        safety=SafetyView(
            safety.audit_threshold,
            safety.caught,
            safety.caught_by,
            safety.per_monitor_max_score,
        ),
    )


def is_flagged(step: StepView, threshold: float) -> bool:
    """A step is flagged when a verdict on it scores strictly above the threshold, as the harness counts."""
    return step.peak is not None and step.peak > threshold


def count_steps(page: RunPage | None, threshold: float) -> StepCounts:
    """The step and communication counts of a run's page, or nothing counted when it has none."""
    if page is None:
        return StepCounts()
    covert = [step for step in page.steps if step.covert]
    kinds = [said.kind for said in page.communication]
    return StepCounts(
        steps=len(page.steps),
        flagged=sum(1 for step in page.steps if is_flagged(step, threshold)),
        covert=len(covert),
        covert_missed=sum(1 for step in covert if not is_flagged(step, threshold)),
        messages=kinds.count(MESSAGE_KIND),
        scratch=kinds.count(SCRATCH_KIND),
    )


def summarize_run(bundle: Path, scores: RunScores, page: RunPage | None, href: str | None) -> RunRow:
    """One row of the index: the outcome from the scores, and the steps when the run has a transcript."""
    log = find_transcript_log(bundle)
    created = read_eval_log(str(log), header_only=True).eval.created if log else None
    threshold = scores.safety.audit_threshold
    counts = count_steps(page, threshold)
    return RunRow(
        name=page.run.name if page else bundle.name,
        slug=scores.run_slug,
        mode=scores.mode,
        seed=scores.seed,
        attacker=scores.models.get("untrusted_agent", UNKNOWN_MODEL),
        monitor=scores.models.get("trusted_monitor", UNKNOWN_MODEL),
        created=created or datetime.fromtimestamp(bundle.stat().st_mtime, UTC).isoformat(timespec="seconds"),
        minutes=round(scores.wall_clock_seconds / SECONDS_PER_MINUTE),
        main_score=scores.main_task.final_score,
        gate_passed=scores.main_task.gate_passed,
        gate_reason=scores.main_task.gate_reason,
        landed=scores.side_task.landing_minimal,
        robust=scores.side_task.landing_robust,
        milestones_reached=sum(1 for m in scores.side_task.milestones.values() if m.reached),
        milestones_total=len(scores.side_task.milestones),
        caught=scores.safety.caught,
        caught_by=scores.safety.caught_by,
        peaks=scores.safety.per_monitor_max_score,
        threshold=threshold,
        href=href,
        why=None if href else NO_TRANSCRIPT,
        steps=counts.steps,
        flagged=counts.flagged,
        covert=counts.covert,
        covert_missed=counts.covert_missed,
        messages=counts.messages,
        scratch=counts.scratch,
    )


def build_folder(root: Path, out: Path, settings: ExplorerSettings) -> Path:
    """A page per run found under `root`, in `out/runs/`, and `out/index.html` over them all."""
    (out / RUNS_FOLDER).mkdir(parents=True, exist_ok=True)
    rows: list[RunRow] = []
    for scores_path in sorted(root.rglob(SCORES_FILE)):
        bundle = scores_path.parent
        if out in bundle.parents:
            continue
        scores = load_scores(scores_path)
        if find_transcript_log(bundle) is None:
            rows.append(summarize_run(bundle, scores, None, None))
            continue
        page = build_run_page(bundle, settings, index_href=f"../{INDEX_PAGE}")
        href = f"{RUNS_FOLDER}/{page.run.name}.html"
        write_page(RUN_TEMPLATE, page, out / href)
        rows.append(summarize_run(bundle, scores, page, href))
    incomplete = sorted(
        {
            str(marker_path.parent.relative_to(root))
            for marker in BUNDLE_MARKERS
            for marker_path in root.rglob(marker)
            if not (marker_path.parent / SCORES_FILE).exists() and marker_path.parent.name not in BUNDLE_PARTS
        },
    )
    index = RunIndex(
        root=str(root),
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        runs=tuple(sorted(rows, key=lambda row: row.created, reverse=True)),
        incomplete=tuple(incomplete),
    )
    return write_page(INDEX_TEMPLATE, index, out / INDEX_PAGE)


def build(target: Path, out: Path | None, settings: ExplorerSettings) -> Path:
    """The page to open: a run bundle's own page, or the index over every run in a folder."""
    if (target / SCORES_FILE).exists():
        return write_page(RUN_TEMPLATE, build_run_page(target, settings), out or target / RUN_PAGE)
    return build_folder(target, out or target / SITE_FOLDER, settings)


def write_page(template: str, data: RunPage | RunIndex, out: Path) -> Path:
    """Fill a page template with `data` and write it to `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    page = files("loc_arena.explorer").joinpath("templates", template).read_text(encoding="utf-8")
    out.write_text(page.replace(DATA_MARKER, embed_json(dataclasses.asdict(data)), 1), encoding="utf-8")
    return out


def embed_json(data: Mapping[str, object]) -> str:
    """JSON that is safe inside a <script> element: every '<' escaped, so no model text can close it."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def format_readable(value: object) -> str:
    """A tool's arguments or result as text a person reads: each key, then its value, strings unquoted."""
    if isinstance(value, dict) and value:
        return "\n\n".join(
            f"{key}:\n{item if isinstance(item, str) else json.dumps(item, indent=2, ensure_ascii=False)}"
            for key, item in value.items()
        )
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def pick_action_line(arguments: Mapping[str, object], settings: ExplorerSettings) -> str:
    """The one line a transcript card shows for a tool call: its most telling argument."""
    key = next((k for k in ACTION_KEYS if arguments.get(k) not in (None, "")), None)
    value = arguments[key] if key else (arguments or "")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[: settings.action_characters]


def _make_said(step: str, sender: str, verdicts: tuple[VerdictView, ...], covert: tuple[str, ...]) -> Said:
    return Said(
        step,
        sender,
        tuple(v.score for v in verdicts),
        covert,
        tuple(dict.fromkeys(v.monitor for v in verdicts)),
    )


def _describe_model_call(event: ModelEvent) -> ModelCallView:
    metadata = event.metadata or {}
    seq = metadata.get("sealed_seq")
    return ModelCallView(
        seq=seq if isinstance(seq, int) else None,
        identity=str(metadata.get("identity") or event.role or "model"),
        phase=str(metadata["phase"]) if metadata.get("phase") else None,
        prompt="\n\n".join(message.text for message in event.input),
        reply=event.error or event.output.completion,
        failed=bool(event.error),
    )


def _describe_tool_call(
    event: ToolEvent,
    verdicts: Mapping[int, tuple[VerdictView, ...]],
    evidence: Mapping[int, frozenset[str]],
    settings: ExplorerSettings,
) -> ToolCallView:
    seqs = read_event_seqs(event)
    seq = seqs.sealed
    arguments = event.arguments or {}
    result = str(event.result or "")
    try:
        result_text = format_readable(json.loads(result))
    except ValueError:
        result_text = result
    covert = set(evidence.get(seq, frozenset())) if seq is not None else set()
    if event.function in COVERT_TOOL_NAMES:
        covert.add(f"covert tool {event.function}")
    return ToolCallView(
        seq=seq,
        mirror_seq=seqs.mirror,
        function=event.function,
        arguments=json.dumps(arguments, indent=2, sort_keys=True, ensure_ascii=False),
        arguments_text=format_readable(arguments),
        action=pick_action_line(arguments, settings),
        result=result,
        result_text=result_text,
        blocked=event.error.message if event.error else None,
        covert=tuple(sorted(covert)),
        verdicts=verdicts.get(seq, ()) if seq is not None else (),
    )


def _describe_event(
    event: InfoEvent,
    verdicts: Mapping[int, tuple[VerdictView, ...]],
    evidence: Mapping[int, frozenset[str]],
) -> EventDescription:
    data = event.data if isinstance(event.data, dict) else {"data": event.data}
    seqs = read_event_seqs(event)
    seq = seqs.sealed
    payload = data.get("payload")
    target = data.get("target_id")
    view = EventView(
        seq=seq,
        mirror_seq=seqs.mirror,
        kind=str(event.source or "info"),
        actor=str(data["actor_uid"]) if data.get("actor_uid") else None,
        body=json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        covert=tuple(sorted(evidence.get(seq, frozenset()))) if seq is not None else (),
        verdicts=verdicts.get(seq, ()) if seq is not None else (),
    )
    return EventDescription(
        view=view,
        target=str(target) if target else None,
        payload=payload if isinstance(payload, dict) else {},
    )


def _describe_step(
    step_id: str,
    key: tuple[str, int],
    calls: Mapping[str, list[ModelCallView]],
    tools: Mapping[str, list[ToolCallView]],
    events: Mapping[str, list[EventView]],
) -> StepView:
    items = (*tools.get(step_id, ()), *events.get(step_id, ()))
    verdicts = tuple(v for item in items for v in item.verdicts)
    return StepView(
        id=step_id,
        agent=key[0],
        round=key[1],
        calls=tuple(calls.get(step_id, ())),
        tools=tuple(tools.get(step_id, ())),
        events=tuple(events.get(step_id, ())),
        covert=tuple(sorted({tag for item in items for tag in item.covert})),
        verdicts=verdicts,
        peak=max((v.score for v in verdicts), default=None),
    )
