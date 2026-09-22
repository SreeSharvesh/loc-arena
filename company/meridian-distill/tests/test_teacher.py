from __future__ import annotations

import pytest

from meridian_common.metrics import MetricsRegistry
from meridian_distill.errors import TeacherError
from meridian_distill.teacher import (
    DEFAULT_FEATURE_DIM,
    TeacherClient,
    compute_features,
    teacher_features,
)
from meridian_serving.api.serve import ServingEngine
from meridian_serving.sampler import Sampler


def _engine() -> ServingEngine:
    return ServingEngine()


def test_teacher_runs_through_serving_engine() -> None:
    client = TeacherClient(_engine(), max_tokens=4)
    response = client.query((1, 2, 3))
    assert response.generated_len == 4
    assert len(response.features) == DEFAULT_FEATURE_DIM
    assert client.calls == 1


def test_teacher_caches_repeated_prompt() -> None:
    client = TeacherClient(_engine(), max_tokens=4)
    first = client.query((7, 8, 9))
    second = client.query((7, 8, 9))
    assert first is second
    assert client.calls == 1
    assert client.cache_hits == 1


def test_distinct_prompts_make_distinct_calls() -> None:
    client = TeacherClient(_engine(), max_tokens=4)
    a = client.query((1, 2, 3))
    b = client.query((10, 11, 12))
    assert client.calls == 2
    assert a.tokens != b.tokens


def test_calls_are_counted_in_shared_metrics_registry() -> None:
    registry = MetricsRegistry()
    client = TeacherClient(_engine(), max_tokens=4, metrics=registry)
    client.query((1, 2, 3))
    client.query((4, 5, 6))
    counter = registry.counter("distill.teacher.calls")
    assert counter.value == 2.0
    assert client.metrics is registry


def test_features_are_deterministic_across_clients() -> None:
    prompt = (3, 1, 4, 1, 5)
    r1 = TeacherClient(_engine(), max_tokens=6).query(prompt)
    r2 = TeacherClient(_engine(), max_tokens=6).query(prompt)
    assert teacher_features(r1) == teacher_features(r2)
    assert r1.tokens == r2.tokens


def test_teacher_features_match_compute() -> None:
    response = TeacherClient(_engine(), max_tokens=4).query((2, 4, 6))
    assert teacher_features(response) == compute_features(response.tokens)


def test_sampler_and_logit_fn_path_drives_serving() -> None:
    def logit_fn(context: tuple[int, ...]) -> list[float]:
        base = sum(context) if context else 1
        return [float((base + i) % 7) for i in range(5)]

    client = TeacherClient(sampler=Sampler(temperature=1.0, top_p=1.0), logit_fn=logit_fn, max_tokens=5)
    response = client.query((1, 2, 3))
    assert response.generated_len == 5
    assert client.calls == 1


def test_requires_engine_or_sampler() -> None:
    with pytest.raises(TeacherError):
        TeacherClient()


def test_rejects_non_positive_max_tokens() -> None:
    with pytest.raises(TeacherError):
        TeacherClient(_engine(), max_tokens=0)


def test_compute_features_zero_vector_for_empty() -> None:
    assert compute_features(()) == tuple(0.0 for _ in range(DEFAULT_FEATURE_DIM))
