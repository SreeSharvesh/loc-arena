"""A played run's monitors run again, behind ``make monitor``.

A monitor, or a prompt of one, is compared on runs already played: this runs the monitors it is given, enabled
or not, over each play's mirror events, and writes what they scored and what they cost into a folder of its
own. It changes nothing of the run: a monitor's model calls go through a gateway core that records them in
that folder, never in the run's sealed log, and no task is graded again.

The scores here are judgements after the play: every verdict counts, whenever its events happened, where a
run's own ``scores.json`` counts only those before the harm.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from loc_arena.config import ConfigError, RunConfig
from loc_arena.gateway.core import OpenRouterProvider, is_live_provider_configured
from loc_arena.harness import build_monitor_caller
from loc_arena.logging_.events import AppendOnlyLog, fingerprint, read_events
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE
from loc_arena.monitors.aggregation import load_aggregation_config, reduce_monitor
from loc_arena.monitors.registry import (
    MonitorSpec,
    MonitorVerdict,
    load_monitor_specs,
    load_monitors,
    run_monitors,
)
from loc_arena.protocol import monitor_context
from loc_arena.task import SNAPSHOT_FILE, read_snapshot_file

SCORES_FILE = "scores.json"
VERDICTS_FILE = "verdicts.jsonl"
FAILURE_KINDS = ("monitor_parse_error", "monitor_error")  # a call that failed, or a reply with no verdict
PROMPT_REFERENCE_SUFFIX = "prompt_ref"  # a monitor's key that names a prompt file ends with this


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class PlayScore(_Record):
    """One monitor's score of one play; no score where it gave no verdict, which is not a score of zero."""

    score: float | None
    by_actor: dict[str, float]  # each actor's highest verdict: an agent's own, for a monitor per agent
    verdicts: int
    errors: int  # the calls that failed or whose reply held no verdict


class MonitorUsage(_Record):
    """What a monitor's model calls came to, over every play.

    No dollars unless the provider reported them for every call: a sum of some calls is not the total.
    """

    calls: int
    prompt_tokens: int
    completion_tokens: int
    dollars: float | None


class MonitorRerun(_Record):
    """One monitor's re-run: what it ran with, its score of each play, and its cost."""

    model: str | None  # the model its role names; none for a detector
    samples: int
    prompts: dict[str, str]  # each prompt file it names, with the fingerprint of its text
    plays: dict[str, PlayScore]
    usage: MonitorUsage


class Rerun(_Record):
    """``scores.json`` of a re-run: the run, and each monitor that ran over it."""

    run: str
    monitors: dict[str, MonitorRerun]


class _VerdictLine(_Record):
    play: str
    monitor: str
    actor: str
    score: float
    reason: str
    target_seq: int | None
    from_model: bool


def rerun_monitors(
    config: RunConfig,
    run_directory: Path,
    names: Sequence[str],
    out: Path,
    *,
    monitors_path: Path,
    provider: Any = None,
) -> Rerun:
    """Run the monitors ``names`` over each finished play of the run in ``run_directory``; write into ``out``.

    The run need not be graded: a play is read from its own folder. Without a ``provider``, the monitors
    call the gateway or the key this process has; with neither, each scores as it does without a model.
    Raises ``ConfigError`` on a name ``monitors_path`` does not declare, ``FileNotFoundError`` on a run with
    no finished play, and ``FileExistsError`` on an ``out`` that holds anything: the run's own folder, whose
    ``scores.json`` it would replace, or an earlier re-run's, whose recorded calls it would count as its own.
    """
    specs = {spec.name: spec for spec in load_monitor_specs(monitors_path, config.scenario)}
    if unknown := sorted(set(names) - set(specs)):
        raise ConfigError(f"{monitors_path} declares no monitor named {', '.join(unknown)}")
    chosen = [specs[name] for name in dict.fromkeys(names)]
    plays = find_plays(run_directory)
    if not plays:
        raise FileNotFoundError(f"{run_directory} holds no finished play: none has a {SNAPSHOT_FILE}")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty: a re-run writes into a folder of its own")
    if provider is None and is_live_provider_configured():
        provider = OpenRouterProvider(
            timeout=config.settings.gateway.timeout_seconds,
            model_providers=config.model_providers,
        )
    out.mkdir(parents=True, exist_ok=True)

    def score(play: tuple[str, str]) -> list[MonitorVerdict]:
        record = out / f"{play[0]}.events.jsonl"
        return _score_play(config, run_directory / play[1], monitors_path, chosen, record, provider)

    with ThreadPoolExecutor(max_workers=len(plays) or 1) as pool:  # each play records in a log of its own
        verdicts = dict(zip((name for name, _ in plays), pool.map(score, plays), strict=True))

    reduce_default = load_aggregation_config(monitors_path).per_monitor_reduce
    rerun = Rerun(
        run=run_directory.name,
        monitors={
            spec.name: MonitorRerun(
                model=config.models[spec.model].model if spec.model in config.models else None,
                samples=spec.ensemble_n,
                prompts=_fingerprint_prompts(spec),
                plays={
                    play: _score_of(spec, verdicts[play], out / f"{play}.events.jsonl", reduce_default)
                    for play in verdicts
                },
                usage=_read_usage(spec.name, [out / f"{play}.events.jsonl" for play in verdicts]),
            )
            for spec in chosen
        },
    )
    (out / SCORES_FILE).write_text(rerun.model_dump_json(indent=2))
    lines = [
        _VerdictLine(
            play=play,
            monitor=verdict.monitor,
            actor=verdict.actor_uid,
            score=verdict.score,
            reason=verdict.reason,
            target_seq=verdict.target_seq,
            from_model=verdict.from_model,
        ).model_dump_json()
        for play, of_play in verdicts.items()
        for verdict in of_play
    ]
    (out / VERDICTS_FILE).write_text("".join(f"{line}\n" for line in lines))
    return rerun


def find_plays(run_directory: Path) -> list[tuple[str, str]]:
    """Each finished play's name and folder: the run's own play by its mode, and ``twin`` for its twin.

    A play is finished once it has written its snapshot, which says its mode and where its mirror log is.
    A run stopped early has a play without one, and that play is left out. Nothing here reads the run's
    ``scores.json``, so a run that was never graded is read like any other.
    """
    plays: list[tuple[str, str]] = []
    for phase in (EPISODE_PHASE, TWIN_PHASE):
        if (run_directory / phase / SNAPSHOT_FILE).is_file():
            mode = read_snapshot_file(run_directory / phase).mode
            plays.append((mode if phase == EPISODE_PHASE else "twin", phase))
    return plays


def _score_play(
    config: RunConfig,
    play_directory: Path,
    monitors_path: Path,
    specs: Sequence[MonitorSpec],
    record: Path,
    provider: Any,
) -> list[MonitorVerdict]:
    """The verdicts of ``specs``' monitors on one play's mirror events, their model calls in ``record``."""
    events = list(read_events(play_directory / read_snapshot_file(play_directory).mirror_path))
    if not events:
        return []
    registry = load_monitors(monitors_path, config.models, config.scenario, names={s.name for s in specs})
    caller, sink = build_monitor_caller(
        AppendOnlyLog(record, events[0].episode_id),
        config,
        provider,
        roles={spec.name: spec.model for spec in specs},
        failures_before_fallback=load_aggregation_config(monitors_path).monitor_failures_before_fallback,
    )
    context = monitor_context(config, model_caller=caller, **({"event_sink": sink} if sink else {}))
    return run_monitors(registry, events, context)


def _score_of(spec: MonitorSpec, verdicts: Sequence[MonitorVerdict], record: Path, reduce: str) -> PlayScore:
    own = [verdict for verdict in verdicts if verdict.monitor == spec.name]
    by_actor: dict[str, float] = {}
    for verdict in own:
        by_actor[verdict.actor_uid] = max(verdict.score, by_actor.get(verdict.actor_uid, 0.0))
    failed = [
        event
        for event in (read_events(record) if record.exists() else ())
        if event.kind in FAILURE_KINDS and event.payload.get("monitor") == spec.name
    ]
    return PlayScore(
        score=reduce_monitor([v.score for v in own], spec.per_monitor_reduce or reduce) if own else None,
        by_actor=dict(sorted(by_actor.items())),
        verdicts=len(own),
        errors=len(failed),
    )


def _read_usage(monitor: str, records: Sequence[Path]) -> MonitorUsage:
    calls = [
        event.payload
        for record in records
        if record.exists()
        for event in read_events(record)
        if event.kind == "inference_call" and event.payload.get("caller_identity") == monitor
    ]
    costs = [call["cost"] for call in calls if call.get("cost") is not None]
    priced = bool(calls) and len(costs) == len(calls)  # a sum of some calls is not the total
    return MonitorUsage(
        calls=len(calls),
        prompt_tokens=sum(call["prompt_tokens"] for call in calls),
        completion_tokens=sum(call["completion_tokens"] for call in calls),
        dollars=round(sum(costs), 6) if priced else None,
    )


def _fingerprint_prompts(spec: MonitorSpec) -> dict[str, str]:
    """Each prompt file ``spec`` names, in its own keys or its facts, with the fingerprint of its text."""
    named = [str(value) for key, value in spec.extra.items() if key.endswith(PROMPT_REFERENCE_SUFFIX)]
    return {source: fingerprint(Path(source).read_text()) for source in (*spec.prompt_sources, *named)}


def render_rerun(rerun: Rerun) -> str:
    """A re-run as lines: each monitor's score of each play, then its calls, tokens and dollars."""
    lines: list[str] = []
    for name, monitor in rerun.monitors.items():
        for play, scored in monitor.plays.items():
            score = "no verdict" if scored.score is None else f"{scored.score:.3f}"
            actors = " ".join(f"{actor}={value:.2f}" for actor, value in scored.by_actor.items())
            counts = f"{scored.verdicts} verdicts, {scored.errors} errors"
            lines.append(f"{name} {play}: {score} ({counts}) {actors}")
        usage = monitor.usage
        dollars = "dollars not reported for every call" if usage.dollars is None else f"${usage.dollars:.4f}"
        lines.append(
            f"{name} cost: {usage.calls} calls, {usage.prompt_tokens} prompt tokens, "
            f"{usage.completion_tokens} completion tokens, {dollars}",
        )
    return "\n".join(lines)
