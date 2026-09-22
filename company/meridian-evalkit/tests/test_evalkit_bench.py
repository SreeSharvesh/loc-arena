"""Tests for the throughput/cost benchmark and its deterministic workload builder."""

from __future__ import annotations

import pytest

from meridian_evalkit.bench import ThroughputBench, build_workload
from meridian_evalkit.errors import BenchError
from meridian_serving.api.serve import ServingEngine


def test_workload_deterministic_for_seed() -> None:
    assert build_workload(20, seed=3) == build_workload(20, seed=3)


def test_workload_differs_across_seeds() -> None:
    assert build_workload(20, seed=1) != build_workload(20, seed=2)


def test_workload_size_and_bounds() -> None:
    wl = build_workload(30, seed=0, min_prompt=4, max_prompt=10)
    assert len(wl) == 30
    assert all(4 <= r.prompt_len <= 10 for r in wl)


def test_workload_bad_size_raises() -> None:
    with pytest.raises(BenchError):
        build_workload(0)


def test_workload_bad_bounds_raises() -> None:
    with pytest.raises(BenchError):
        build_workload(4, min_prompt=10, max_prompt=2)


def test_bench_reports_cost() -> None:
    wl = build_workload(24, seed=5)
    result = ThroughputBench(ServingEngine(batch_size=8)).run(wl)
    assert result.num_requests == 24
    assert result.schedule_cost > 0
    assert result.cost_per_generated_token == pytest.approx(result.schedule_cost / result.total_generated)


def test_bench_deterministic() -> None:
    wl = build_workload(16, seed=2)
    first = ThroughputBench(ServingEngine()).run(wl)
    second = ThroughputBench(ServingEngine()).run(wl)
    assert first == second


def test_bench_empty_workload_raises() -> None:
    with pytest.raises(BenchError):
        ThroughputBench(ServingEngine()).run([])


def test_bench_cost_per_request() -> None:
    wl = build_workload(10, seed=7)
    result = ThroughputBench(ServingEngine()).run(wl)
    assert result.cost_per_request == pytest.approx(result.schedule_cost / result.admitted)
