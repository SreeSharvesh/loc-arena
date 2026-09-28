"""A run's model usage is totalled per model role from the sealed log, and reported in its scores.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import run_episode
from loc_arena.logging_.events import Event, EventKind, read_events
from loc_arena.logging_.model_usage import RunModelUsage, UsageTotal, summarize_model_usage
from loc_arena.stack.contracts import ModelCallUsage
from pydantic import TypeAdapter

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
CALIBRATION_DIRECTORY = "honest_cal"  # where an attack run keeps its honest twin's logs
SEALED_LOG = "events.sealed.jsonl"
RUN_MODEL_USAGE = TypeAdapter(RunModelUsage)


def _event(seq: int, kind: EventKind, role: str, usage: ModelCallUsage) -> Event:
    return Event(
        episode_id="ep",
        seq=seq,
        ts=0.0,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind=kind,
        payload={"model_role": role, **usage.model_dump()},
    )


def _call(seq: int, role: str, usage: ModelCallUsage) -> Event:
    return _event(seq, "inference_call", role, usage)


TEACHER_CALL = ModelCallUsage(prompt_tokens=10, completion_tokens=5, cached_tokens=4, cost=0.25)
AGENT_CALL = ModelCallUsage(prompt_tokens=20, completion_tokens=8, cached_tokens=6, cost=0.5)
UNCOSTED_CALL = ModelCallUsage(prompt_tokens=1, completion_tokens=1, cached_tokens=0, cost=None)
MIXED_ROLES = (
    _call(0, "teacher", TEACHER_CALL),
    _call(1, "untrusted_agent", AGENT_CALL),
    _call(2, "teacher", TEACHER_CALL),
)


def _completed_calls(sealed_log: Path) -> int:
    return sum(event.kind == "inference_call" for event in read_events(sealed_log))


def _model_usage(bundle: Path) -> RunModelUsage:
    scores = json.loads((bundle / "scores.json").read_text())
    return RUN_MODEL_USAGE.validate_python(scores["model_usage"])


@pytest.fixture(scope="module")
def attack_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return run_episode(CONFIG, mode="attack", out_root=tmp_path_factory.mktemp("attack"), write_report=False)


def test_each_model_roles_calls_are_totalled_apart() -> None:
    usage = summarize_model_usage(MIXED_ROLES)

    assert usage.by_role == {
        "teacher": UsageTotal(
            completed_calls=2,
            prompt_tokens=20,
            completion_tokens=10,
            cached_tokens=8,
            total_tokens=30,
            cost=0.5,
        ),
        "untrusted_agent": UsageTotal(
            completed_calls=1,
            prompt_tokens=20,
            completion_tokens=8,
            cached_tokens=6,
            total_tokens=28,
            cost=0.5,
        ),
    }


def test_the_total_sums_the_calls_of_every_role() -> None:
    usage = summarize_model_usage(MIXED_ROLES)

    assert usage.total == UsageTotal(
        completed_calls=3,
        prompt_tokens=40,
        completion_tokens=18,
        cached_tokens=14,
        total_tokens=58,
        cost=1.0,
    )


def test_the_cost_is_unknown_when_a_call_reported_none() -> None:
    usage = summarize_model_usage((_call(0, "teacher", TEACHER_CALL), _call(1, "teacher", UNCOSTED_CALL)))

    assert usage.total.cost is None


def test_events_other_than_completed_model_calls_are_not_counted() -> None:
    events = (_call(0, "teacher", TEACHER_CALL), _event(1, "monitor_verdict", "teacher", TEACHER_CALL))

    usage = summarize_model_usage(events)

    assert usage.total.completed_calls == 1


def test_a_runs_scores_count_the_episodes_completed_calls(attack_bundle: Path) -> None:
    usage = _model_usage(attack_bundle)

    assert usage.episode.total.completed_calls == _completed_calls(attack_bundle / SEALED_LOG)


def test_an_attack_runs_scores_count_its_calibration_twins_calls_apart(attack_bundle: Path) -> None:
    usage = _model_usage(attack_bundle)

    twin_log = attack_bundle / CALIBRATION_DIRECTORY / SEALED_LOG
    assert usage.calibration is not None
    assert usage.calibration.total.completed_calls == _completed_calls(twin_log)


def test_an_honest_runs_scores_report_no_separate_calibration_twin(tmp_path: Path) -> None:
    bundle = run_episode(CONFIG, mode="honest", out_root=tmp_path, write_report=False)

    assert _model_usage(bundle).calibration is None
