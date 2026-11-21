"""The run reader: a pi agent that reads one finished run and reports what its agents did.

A run's records are text that untrusted agents wrote, and the reader has a shell. So it is isolated as an
agent's loop is: it calls its model through the model gateway with a token of its own, and its container is
on the network with no route out. It mounts the run directory read-only, and one folder for its report. The
provider key stays in the gateway's container, and the gateway records the reader's calls.

The report (``readings/<brief>.md`` in the run directory) opens with facts this module counts from the
records (``summarize_run``), so its numbers do not depend on the reader; the reader's account follows. The
briefs are in ``reader_briefs/``: each says where every file is and how the logs join.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.explorer.scores import load_scores
from loc_arena.logging_.events import read_events
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE
from loc_arena.monitors.registry import MonitorVerdict
from loc_arena.reader_execution import (  # noqa: F401 - preserve the reader's existing public imports
    ASK,
    READER,
    READER_TOKEN_VARIABLE,
    READINGS,
    issue_reader_secrets,
    read_report,
    render_reader_compose,
    run_reader_process,
    settle_reading,
    write_reader_home,
)
from loc_arena.reading_check import (
    CHECKS_FILE,
    CheckLimits,
    ReaderChecks,
    check_answer,
    parse_answer,
    render_reading,
)
from loc_arena.run_facts import RunEvidence, find_names, gather_evidence, write_evidence
from loc_arena.settings import LocArenaSettings
from loc_arena.taxonomy import ReaderTaxonomy, load_taxonomy, observe_run, taxonomy_facts

_TAXONOMY = "taxonomy.json"  # in the reader's home: the categories and code-observed instances


def _spread(verdicts: Iterable[MonitorVerdict], high: float) -> str:
    """Each monitor's verdicts: how many, how many above ``high``, and the highest."""
    by_monitor: dict[str, list[float]] = {}
    for verdict in verdicts:
        by_monitor.setdefault(verdict.monitor, []).append(verdict.score)
    return "; ".join(
        f"{monitor}: verdicts {len(scores)}, above {high} {sum(score > high for score in scores)}, "
        f"highest {max(scores)}"
        for monitor, scores in sorted(by_monitor.items())
    )


def summarize_run(run_directory: Path) -> str:
    """The facts a reading opens with, counted from the run's records: Markdown."""
    scores = load_scores(run_directory / "scores.json")
    rows = []
    for play, phase in ((scores.mode, EPISODE_PHASE), ("twin", TWIN_PHASE)):
        sealed = run_directory / phase / "events.sealed.jsonl"
        if not sealed.is_file():  # an honest run has no twin
            continue
        kinds = Counter(event.kind for event in read_events(sealed))
        counts = (sum(kinds.values()), *(kinds[kind] for kind in ("job", "message", "action", "pr")))
        ends = run_directory / phase / "session_ends.json"
        recorded = json.loads(ends.read_text()).values() if ends.is_file() else []
        reasons = Counter(end["reason"] for end in recorded)
        ended = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items())) or "not recorded"
        rows.append(f"| {play} | {' | '.join(map(str, counts))} | {ended} |")
    main, side, safety, twin = scores.main_task, scores.side_task, scores.safety, scores.honest_twin
    high = safety.audit_threshold  # the run's own: a verdict above it is flagged
    reached = sorted(name for name, milestone in side.milestones.items() if milestone.reached)
    return "\n".join(
        [
            "## Facts counted from the records",
            "",
            "| Play | Sealed events | Job events | Messages | Actions | Pull request events | Sessions |",
            "|---|---|---|---|---|---|---|",
            *rows,
            "",
            f"- Main task: score {main.final_score:.3f}, gate {main.gate_reason}.",
            f"- Side task: landed {side.landing_minimal}; milestones: {', '.join(reached) or 'none'}.",
            f"- Caught: {safety.caught}; overall suspicion {safety.overall_suspicion}, "
            f"threshold {safety.aggregation.threshold if safety.aggregation else 'not recorded'}.",
            f"- Run verdicts: {_spread(scores.monitor_verdicts, high) or 'none'}.",
            f"- Twin verdicts: {(_spread(twin.monitor_verdicts, high) or 'none') if twin else 'no twin'}.",
            "",
        ],
    )


def gather_reading_evidence(
    run_directory: Path,
    settings: LocArenaSettings,
    question: str | None,
) -> tuple[RunEvidence, ReaderTaxonomy]:
    """What a reading's claims are checked against, and the taxonomy the reader is given.

    The run's evidence, what code observed of the taxonomy as facts and its categories as names, and the names
    of the owner's question, which a claim may repeat (a key the run lacks).
    """
    evidence, taxonomy = gather_evidence(run_directory, settings.reader.word_fields), load_taxonomy()
    observed = observe_run(
        run_directory,
        taxonomy,
        str(settings.gateway.secrets_dir),
        settings.reader.repeat_at_least,
    )
    evidence = evidence.model_copy(
        update={
            "facts": evidence.facts | taxonomy_facts(taxonomy, observed),
            "names": evidence.names | {category.id for category in taxonomy.categories},
            "asked": frozenset(find_names(question)) if question else frozenset(),
        },
    )
    given = ReaderTaxonomy(
        version=taxonomy.version,
        categories=taxonomy.categories,
        instances=observed.instances,
    )
    return evidence, given


def name_reading(brief: str, question: str | None) -> str:
    """A reading's name in the run's readings: its brief's, or ``ask-`` and its question's hash."""
    return f"{ASK}-{hashlib.sha256(question.encode()).hexdigest()[:8]}" if question else brief


def read_run(run_directory: Path, brief: str, config: RunConfig, question: str | None = None) -> Path:
    """Have the reader read ``run_directory`` with ``brief``; the checked reading's path in its readings.

    The reader is given the run's facts and its events' words, and writes its claims as JSON; each claim
    that fails a check is dropped (``reading_check``). ``readings/<name>.unchecked.json`` keeps what it wrote.
    An earlier reading of the same name is replaced. Raises ``ValueError`` when the directory holds no graded
    run, no brief has that name, a question is missing or misplaced, or the models file has no reader role,
    and ``RuntimeError`` when the gateway does not start, the reader fails or runs past its timeout, its calls
    cannot be copied out, or its answer cannot be kept or is not the JSON its brief asks for.
    """
    run_directory = run_directory.resolve()
    if not (run_directory / "scores.json").is_file():
        raise ValueError(f"{run_directory} holds no scores.json: it is no graded run's directory")
    settings = settle_reading(config)
    # Before any model call: a run it cannot count is not read.
    facts, (evidence, taxonomy) = (
        summarize_run(run_directory),
        gather_reading_evidence(run_directory, settings, question),
    )
    limits = CheckLimits(**settings.reader.model_dump(include=set(CheckLimits.model_fields)))
    readings, name = run_directory / READINGS, name_reading(brief, question)
    report, calls, kept = (
        readings / f"{name}{suffix}" for suffix in (".md", ".calls.jsonl", ".unchecked.json")
    )
    with tempfile.TemporaryDirectory(prefix="locarena-reading-") as scratch:
        home, work = Path(scratch, "home"), Path(scratch, "work")
        work.mkdir()
        command = write_reader_home(home, config, brief, question)
        write_evidence(evidence, home)
        (home / _TAXONOMY).write_text(
            taxonomy.model_dump_json(exclude={"categories": {"__all__": {"detector"}}}),
        )
        (home / CHECKS_FILE).write_text(
            ReaderChecks(
                evidence=evidence,
                settings=CheckLimits(**settings.reader.model_dump(include=set(CheckLimits.model_fields))),
            ).model_dump_json(),
        )
        written = run_reader_process(config, run_directory, home, work, command, calls, (report, kept))
    kept.write_text(written)
    answer = parse_answer(written)
    checked = check_answer(answer, evidence, limits)
    report.write_text(render_reading(checked, None if question else facts))
    return report
