"""A run's model usage and failed calls are totalled per model role from its sealed log, into scores.json."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import Message, ProviderError, ProviderResult, ToolSpec
from loc_arena.harness import run_episode
from loc_arena.logging_.events import Event, EventKind, read_events
from loc_arena.logging_.model_usage import FailureReason, RunModelUsage, UsageTotal, summarize_model_usage
from loc_arena.stack.contracts import ModelCallFailure, ModelCallUsage
from pydantic import JsonValue, TypeAdapter

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MONITORS = "configs/monitors.yaml"  # every model-backed monitor in it is live
CALIBRATION_DIRECTORY = "honest_cal"  # where an attack run keeps its honest twin's logs
SEALED_LOG = "events.sealed.jsonl"
RUN_MODEL_USAGE = TypeAdapter(RunModelUsage)


class FailingProvider:
    """The provider boundary, failing every call as OpenRouter does once its retries are spent."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        raise ProviderError(f"{model}: the provider kept answering 502", status_code=HTTPStatus.BAD_GATEWAY)


def _event(seq: int, kind: EventKind, payload: dict[str, JsonValue]) -> Event:
    return Event(
        episode_id="ep",
        seq=seq,
        ts=0.0,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind=kind,
        payload=payload,
    )


def _call(seq: int, role: str, usage: ModelCallUsage) -> Event:
    return _event(seq, "inference_call", {"model_role": role, **usage.model_dump()})


def _failure(seq: int, role: str, failure: ModelCallFailure) -> Event:
    payload: dict[str, JsonValue] = {"model_role": role, "caller_identity": "agent-main"}
    return _event(seq, "inference_error", payload | {"failure": failure.model_dump()})


TEACHER_CALL = ModelCallUsage(prompt_tokens=10, completion_tokens=5, cached_tokens=4, cost=0.25)
AGENT_CALL = ModelCallUsage(prompt_tokens=20, completion_tokens=8, cached_tokens=6, cost=0.5)
UNCOSTED_CALL = ModelCallUsage(prompt_tokens=1, completion_tokens=1, cached_tokens=0, cost=None)
BAD_GATEWAY = ModelCallFailure(status_code=HTTPStatus.BAD_GATEWAY, message="Provider returned error")
TIMEOUT = ModelCallFailure(status_code=None, message="no completion within the call's budget")
MIXED_ROLES = (
    _call(0, "teacher", TEACHER_CALL),
    _call(1, "untrusted_agent", AGENT_CALL),
    _call(2, "teacher", TEACHER_CALL),
)
MIXED_FAILURES = (
    _call(0, "teacher", TEACHER_CALL),
    _failure(1, "untrusted_agent", TIMEOUT),
    _failure(2, "untrusted_agent", BAD_GATEWAY),
    _failure(3, "teacher", BAD_GATEWAY),
)


def _count(sealed_log: Path, kind: EventKind) -> int:
    return sum(event.kind == kind for event in read_events(sealed_log))


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
            failed_calls=0,
            prompt_tokens=20,
            completion_tokens=10,
            cached_tokens=8,
            total_tokens=30,
            cost=0.5,
        ),
        "untrusted_agent": UsageTotal(
            completed_calls=1,
            failed_calls=0,
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
        failed_calls=0,
        prompt_tokens=40,
        completion_tokens=18,
        cached_tokens=14,
        total_tokens=58,
        cost=1.0,
    )


def test_the_cost_is_unknown_when_a_call_reported_none() -> None:
    usage = summarize_model_usage((_call(0, "teacher", TEACHER_CALL), _call(1, "teacher", UNCOSTED_CALL)))

    assert usage.total.cost is None


def test_events_other_than_model_calls_are_not_counted() -> None:
    events = (_call(0, "teacher", TEACHER_CALL), _event(1, "monitor_verdict", {"model_role": "teacher"}))

    usage = summarize_model_usage(events)

    assert usage.total.completed_calls == 1


def test_failed_calls_are_counted_under_their_role() -> None:
    usage = summarize_model_usage(MIXED_FAILURES)

    assert {role: total.failed_calls for role, total in usage.by_role.items()} == {
        "teacher": 1,
        "untrusted_agent": 2,
    }


def test_the_total_counts_the_failed_calls_of_every_role() -> None:
    usage = summarize_model_usage(MIXED_FAILURES)

    assert usage.total.failed_calls == len(MIXED_FAILURES) - 1


def test_failures_with_the_same_status_and_message_are_one_reason() -> None:
    usage = summarize_model_usage(MIXED_FAILURES)

    assert set(usage.failure_reasons) == {
        FailureReason(status_code=HTTPStatus.BAD_GATEWAY, message=BAD_GATEWAY.message, failed_calls=2),
        FailureReason(status_code=None, message=TIMEOUT.message, failed_calls=1),
    }


def test_the_most_frequent_failure_reason_comes_first() -> None:
    usage = summarize_model_usage(MIXED_FAILURES)

    assert usage.failure_reasons[0].message == BAD_GATEWAY.message


def test_a_runs_scores_count_the_episodes_completed_calls(attack_bundle: Path) -> None:
    usage = _model_usage(attack_bundle)

    assert usage.episode.total.completed_calls == _count(attack_bundle / SEALED_LOG, "inference_call")


def test_an_attack_runs_scores_count_its_calibration_twins_calls_apart(attack_bundle: Path) -> None:
    usage = _model_usage(attack_bundle)

    twin_log = attack_bundle / CALIBRATION_DIRECTORY / SEALED_LOG
    assert usage.calibration is not None
    assert usage.calibration.total.completed_calls == _count(twin_log, "inference_call")


def test_an_honest_runs_scores_report_no_separate_calibration_twin(tmp_path: Path) -> None:
    bundle = run_episode(CONFIG, mode="honest", out_root=tmp_path, write_report=False)

    assert _model_usage(bundle).calibration is None


def test_a_runs_scores_count_the_episodes_failed_calls(tmp_path: Path) -> None:
    bundle = run_episode(
        CONFIG,
        mode="honest",
        out_root=tmp_path,
        monitors_path=MONITORS,
        monitor_provider=FailingProvider(),
        write_report=False,
    )

    failed = _count(bundle / SEALED_LOG, "inference_error")
    assert failed > 0  # the live monitors called the failing provider
    assert _model_usage(bundle).episode.total.failed_calls == failed
