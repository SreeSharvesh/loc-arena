"""Read a graded directory without importing the engine or changing its records."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from run_insights.models import Agent, Analysis, Judgment, Outcome, Record, RunData
from run_insights.validation import validate_draft

_OBJECT = TypeAdapter(dict[str, JsonValue])


class _Source(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class _Event(_Source):
    seq: int = Field(ge=0)
    actor_uid: str
    kind: str
    ts: float
    turn: int = 0
    tool: str | None = None
    target_id: str | None = None


class _Verdict(_Source):
    monitor: str
    score: float
    reason: str
    target_seq: int | None = None


class _Main(_Source):
    final_score: float
    gate_passed: bool | None = None
    gate_reason: str | None = None


class _Milestone(_Source):
    reached: bool


class _Side(_Source):
    landing_minimal: bool
    milestones: dict[str, _Milestone] = Field(default_factory=dict)


class _Aggregation(_Source):
    threshold: float


class _Safety(_Source):
    caught: bool
    audit_threshold: float | None = None
    overall_suspicion: float | None = None
    aggregation: _Aggregation | None = None


class _Twin(_Source):
    overall_suspicion: float | None = None
    monitor_verdicts: list[_Verdict] = Field(default_factory=list)


class _Scores(_Source):
    run_name: str | None = None
    mode: str = "attack"
    main_task: _Main
    side_task: _Side
    safety: _Safety
    honest_twin: _Twin | None = None
    monitor_verdicts: list[_Verdict] = Field(default_factory=list)
    failed_model_calls: dict[str, int] = Field(default_factory=dict)


class _Agent(_Source):
    id: str
    brief: str | None = None
    covert_brief: str | None = None
    brief_ref: str | None = None
    covert_brief_ref: str | None = None


class _Config(_Source):
    agents: list[_Agent] = Field(default_factory=list)
    prompts_inline: dict[str, str | list[str]] = Field(default_factory=dict)


class _Session(_Source):
    type: str
    id: str | None = None
    timestamp: str | float | None = None
    message: dict[str, JsonValue] = Field(default_factory=dict)


def analysis_path(bundle: Path) -> Path:
    """Return the saved analysis path inside a graded bundle."""
    return bundle / "readings" / "insights.json"


def load_analysis(bundle: Path, run: RunData) -> Analysis | None:
    """Read valid analysis for these exact inputs; a stale or absent analysis is unavailable."""
    path = analysis_path(bundle)
    if not path.exists():
        return None
    analysis = Analysis.model_validate_json(path.read_text(encoding="utf-8"))
    if analysis.source_fingerprint != run.fingerprint:
        return None
    validate_draft(analysis, run)
    return analysis


def load_run(bundle: Path) -> RunData:
    """Load both plays, retaining complete original objects and separate sequence namespaces."""
    bundle = bundle.resolve(strict=True)
    _check_source(bundle, bundle / "scores.json")
    raw_scores = _read_object(bundle / "scores.json")
    scores = _Scores.model_validate(raw_scores)
    warnings: list[str] = []
    inputs = [bundle / "scores.json"]
    agents = _load_agents(bundle, inputs, warnings)
    play = "honest" if scores.mode == "honest" else "attack"
    records = _load_play(bundle, "episode", play, inputs, warnings)
    if scores.honest_twin is not None or (bundle / "honest_cal").is_dir():
        records += _load_play(bundle, "honest_cal", "twin", inputs, warnings)
    _attach_verdicts(records, scores.monitor_verdicts, play, warnings)
    if scores.honest_twin is not None:
        _attach_verdicts(records, scores.honest_twin.monitor_verdicts, "twin", warnings)
    if len({record.id for record in records}) != len(records):
        raise ValueError("run records contain duplicate identifiers within a source stream")
    if any(record.kind == "inference_call" for record in records):
        warnings.append(
            "Inference events contain fingerprints, not full messages; session records carry "
            "available messages. Gateway requests and responses are not loaded.",
        )
    return RunData(
        name=scores.run_name or bundle.name,
        fingerprint=_fingerprint(bundle, inputs),
        records=records,
        outcome=_describe_outcome(scores),
        agents=agents,
        warnings=warnings,
        scores=raw_scores,
    )


def _read_object(path: Path) -> dict[str, JsonValue]:
    return _OBJECT.validate_json(path.read_text(encoding="utf-8"), strict=True)


def _read_lines(path: Path) -> list[dict[str, JsonValue]]:
    objects = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if line.strip():
                try:
                    objects.append(_OBJECT.validate_json(line, strict=True))
                except ValueError as error:
                    raise ValueError(f"{path}:{number}: invalid JSON record") from error
    return objects


def _load_agents(bundle: Path, inputs: list[Path], warnings: list[str]) -> list[Agent]:
    path = bundle / "config.resolved.yaml"
    if not path.exists():
        warnings.append("Resolved config is absent; initial agent briefs are unavailable.")
        return []
    inputs.append(path)
    _check_source(bundle, path)
    config = _Config.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    return [
        Agent(
            id=agent.id,
            brief=agent.brief or _resolve_prompt(agent.brief_ref, config),
            covert_brief=agent.covert_brief or _resolve_prompt(agent.covert_brief_ref, config),
        )
        for agent in config.agents
    ]


def _resolve_prompt(reference: str | None, config: _Config) -> str:
    if reference is None:
        return ""
    fragments: list[str] = []
    pending: list[tuple[str, frozenset[str]]] = [(reference, frozenset())]
    while pending:
        current, active = pending.pop()
        if not current.startswith("prompts_inline."):
            raise ValueError(f"invalid prompt reference: {current}")
        name = current.removeprefix("prompts_inline.")
        if name not in config.prompts_inline or name in active:
            raise ValueError(f"missing or cyclic prompt reference: {current}")
        entry = config.prompts_inline[name]
        if isinstance(entry, str):
            fragments.append(entry)
        else:
            pending.extend((child, active | {name}) for child in reversed(entry))
    return "\n\n".join(fragments)


def _load_play(
    bundle: Path,
    folder: str,
    play: Literal["attack", "honest", "twin"],
    inputs: list[Path],
    warnings: list[str],
) -> list[Record]:
    directory = bundle / folder
    records = []
    for stream in ("sealed", "mirror"):
        path = directory / f"events.{stream}.jsonl"
        if folder == "episode" and not path.exists():
            path = bundle / path.name
        if not path.exists():
            warnings.append(f"{play}: {stream} events are absent.")
            continue
        inputs.append(path)
        _check_source(bundle, path)
        for raw in _read_lines(path):
            event = _Event.model_validate(raw)
            records.append(
                Record(
                    id=f"{play}{'-mirror' if stream == 'mirror' else ''}:{event.seq}",
                    actor=event.actor_uid,
                    kind=event.kind,
                    tool=event.tool,
                    target=event.target_id,
                    time=event.ts,
                    turn=event.turn,
                    play=play,
                    stream=stream,
                    raw=raw,
                ),
            )
    sessions = sorted((directory / "loops").glob("*/.pi/session.jsonl"))
    if folder == "episode" and not sessions:
        sessions = sorted((bundle / "loops").glob("*/.pi/session.jsonl"))
    if not sessions:
        warnings.append(f"{play}: pi session files are absent; full model messages may be unavailable.")
    for path in sessions:
        inputs.append(path)
        _check_source(bundle, path)
        records.extend(_load_session(path, play))
    return records


def _load_session(path: Path, play: Literal["attack", "honest", "twin"]) -> list[Record]:
    actor = path.parent.parent.name
    records = []
    turn = 0
    for index, raw in enumerate(_read_lines(path), 1):
        entry = _Session.model_validate(raw)
        role = entry.message.get("role")
        if role == "assistant":
            turn += 1
        tool = entry.message.get("toolName")
        target = entry.message.get("toolCallId")
        records.append(
            Record(
                id=f"{play}-session:{actor}:{entry.id or index}",
                actor=actor,
                kind=f"message:{role}" if role else f"session:{entry.type}",
                tool=tool if isinstance(tool, str) else None,
                target=target if isinstance(target, str) else None,
                time=_read_timestamp(entry.timestamp),
                turn=turn,
                play=play,
                stream="session",
                raw=raw,
            ),
        )
    return records


def _read_timestamp(value: str | float | None) -> float:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return value or 0.0


def _attach_verdicts(
    records: list[Record],
    verdicts: list[_Verdict],
    play: str,
    warnings: list[str],
) -> None:
    by_id = {record.id: record for record in records}
    paired = _pair_records(records, play)
    unmatched: set[str] = set()
    for verdict in verdicts:
        if verdict.target_seq is None:
            warnings.append(f"{play}: {verdict.monitor} has a whole-run verdict without an event target.")
            continue
        target = f"{play}-mirror:{verdict.target_seq}"
        if target not in by_id:
            warnings.append(f"{play}: {verdict.monitor} targets unavailable mirror record {target}.")
            continue
        judgment = Judgment(
            monitor=verdict.monitor,
            score=verdict.score,
            reason=verdict.reason,
            target_id=target,
        )
        by_id[target].verdicts.append(judgment)
        if target in paired:
            by_id[paired[target]].verdicts.append(judgment)
        else:
            unmatched.add(target)
    if unmatched:
        warnings.append(
            f"{play}: {len(unmatched)} judged mirror records have no unique identical sealed "
            "counterpart; their judgments remain on mirror records only.",
        )


def _pair_records(records: list[Record], play: str) -> dict[str, str]:
    sealed: dict[str, list[str]] = defaultdict(list)
    mirror: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if record.play != play or record.stream == "session":
            continue
        content = {key: value for key, value in record.raw.items() if key not in {"seq", "fp"}}
        key = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
        (mirror if record.stream == "mirror" else sealed)[key].append(record.id)
    return {
        names[0]: sealed[key][0]
        for key, names in mirror.items()
        if len(names) == 1 and len(sealed.get(key, [])) == 1
    }


def _describe_outcome(scores: _Scores) -> Outcome:
    return Outcome(
        main_score=scores.main_task.final_score,
        gate_passed=scores.main_task.gate_passed,
        gate_reason=scores.main_task.gate_reason,
        landed=scores.side_task.landing_minimal,
        caught=scores.safety.caught,
        threshold=(
            scores.safety.aggregation.threshold
            if scores.safety.aggregation
            else scores.safety.audit_threshold
        ),
        run_suspicion=scores.safety.overall_suspicion,
        twin_suspicion=scores.honest_twin.overall_suspicion if scores.honest_twin else None,
        milestones={name: milestone.reached for name, milestone in scores.side_task.milestones.items()},
        failed_calls=scores.failed_model_calls,
    )


def _fingerprint(bundle: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(bundle)).encode("utf-8") + b"\0")
        with path.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    return digest.hexdigest()


def _check_source(bundle: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(bundle):
        raise ValueError(f"source escapes the selected run directory: {path}")
    for current in (path, *path.parents):
        if current == bundle:
            break
        if current.is_symlink():
            raise ValueError(f"source contains a symbolic link: {current}")
