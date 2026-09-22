"""Tests for the serving-driven eval runners and the deterministic item scheduler."""

from __future__ import annotations

import pytest

from meridian_evalkit.errors import RunnerError
from meridian_evalkit.harness import EvalItem
from meridian_evalkit.runners import (
    EngineRunner,
    ItemScheduler,
    ParallelRunner,
    SequentialRunner,
)
from meridian_serving.api.serve import ServingEngine


def _items(n: int) -> list[EvalItem]:
    return [
        EvalItem(item_id=f"q{i}", prompt=(1, 2, i % 6, (i * 3) % 7), reference=(i % 5,)) for i in range(n)
    ]


def test_scheduler_round_robin_assignment() -> None:
    lanes = ItemScheduler(3).assign(7)
    assert [a.lane for a in lanes] == [0, 1, 2, 0, 1, 2, 0]
    assert [a.item_index for a in lanes] == list(range(7))


def test_scheduler_execution_order_drains_lanes() -> None:
    order = ItemScheduler(3).execution_order(7)
    # lane 0: 0,3,6 ; lane 1: 1,4 ; lane 2: 2,5
    assert order == [0, 3, 6, 1, 4, 2, 5]


def test_scheduler_single_lane_is_identity() -> None:
    assert ItemScheduler(1).execution_order(5) == [0, 1, 2, 3, 4]


def test_scheduler_visits_every_item_once() -> None:
    order = ItemScheduler(4).execution_order(10)
    assert sorted(order) == list(range(10))


def test_scheduler_bad_lane_count_raises() -> None:
    with pytest.raises(RunnerError):
        ItemScheduler(0)


def test_sequential_runner_produces_predictions() -> None:
    result = SequentialRunner(ServingEngine()).run(_items(8))
    assert result.size == 8
    assert result.execution_order == tuple(f"q{i}" for i in range(8))


def test_parallel_equals_sequential() -> None:
    items = _items(12)
    seq = SequentialRunner(ServingEngine()).run(items)
    par = ParallelRunner(ServingEngine(), num_lanes=4).run(items)
    assert par.predictions == seq.predictions
    assert par.schedule_cost == seq.schedule_cost
    assert par.served_checksum == seq.served_checksum


def test_parallel_execution_order_follows_lanes() -> None:
    items = _items(6)
    par = ParallelRunner(ServingEngine(), num_lanes=3).run(items)
    assert par.execution_order == ("q0", "q3", "q1", "q4", "q2", "q5")


def test_runner_is_deterministic() -> None:
    items = _items(10)
    first = ParallelRunner(ServingEngine(), num_lanes=3).run(items)
    second = ParallelRunner(ServingEngine(), num_lanes=3).run(items)
    assert first == second


def test_runner_empty_items_raises() -> None:
    with pytest.raises(RunnerError):
        SequentialRunner(ServingEngine()).run([])


def test_runner_bad_max_tokens_raises() -> None:
    with pytest.raises(RunnerError):
        EngineRunner(ServingEngine(), max_tokens=0)


def test_runner_predictions_match_direct_engine() -> None:
    items = _items(5)
    result = SequentialRunner(ServingEngine(), max_tokens=8).run(items)
    from meridian_serving.types import Request

    direct = ServingEngine().serve(
        [
            Request(request_id=it.item_id, prompt=it.prompt, max_tokens=8, arrival_seq=i)
            for i, it in enumerate(items)
        ]
    )
    by_id = {r.request_id: r.tokens for r in direct.responses}
    for pred in result.predictions:
        assert pred.tokens == by_id[pred.item_id]
