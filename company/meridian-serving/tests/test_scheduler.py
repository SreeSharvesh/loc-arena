from __future__ import annotations

import pytest

from meridian_serving.scheduler import BatchScheduler, ContinuousBatcher
from meridian_serving.types import Request


def _req(rid: str, prompt_len: int) -> Request:
    return Request(rid, tuple(range(prompt_len)), max_tokens=4)


def test_forms_batches_of_at_most_batch_size() -> None:
    sched = BatchScheduler(batch_size=3)
    plan = sched.form_batches([_req(f"r{i}", 4) for i in range(7)])
    assert [b.size for b in plan.batches] == [3, 3, 1]


def test_pads_to_global_max_width() -> None:
    sched = BatchScheduler(batch_size=2)
    reqs = [_req("a", 2), _req("b", 2), _req("c", 8), _req("d", 2)]
    plan = sched.form_batches(reqs)
    # every batch is padded to the global max prompt length (8), including the all-short first batch
    assert all(b.padded_len == 8 for b in plan.batches)
    assert plan.total_padded_tokens == 4 * 8  # 4 requests * width 8


def test_padding_waste_is_reported() -> None:
    sched = BatchScheduler(batch_size=4)
    reqs = [_req("a", 2), _req("b", 8)]
    plan = sched.form_batches(reqs)
    assert plan.total_real_tokens == 10 and plan.padding_waste == plan.total_padded_tokens - 10


def test_empty_schedule() -> None:
    plan = BatchScheduler().form_batches([])
    assert plan.batches == () and plan.total_padded_tokens == 0


def test_step_cost_matches_plan() -> None:
    sched = BatchScheduler(batch_size=2)
    reqs = [_req("a", 3), _req("b", 5), _req("c", 5)]
    assert sched.step_cost(reqs) == sched.form_batches(reqs).total_padded_tokens


def test_invalid_batch_size() -> None:
    with pytest.raises(ValueError):
        BatchScheduler(batch_size=0)


def test_continuous_batcher_respects_token_budget() -> None:
    batcher = ContinuousBatcher(token_budget=20, max_batch=10)
    reqs = [_req("a", 5), _req("b", 5), _req("c", 5), _req("d", 5)]
    admitted, deferred = batcher.fill(reqs)
    # width grows to 5; cost = width * count; 5*4=20 fits exactly
    assert len(admitted) == 4 and deferred == []


def test_continuous_batcher_defers_when_over_budget() -> None:
    batcher = ContinuousBatcher(token_budget=12)
    reqs = [_req("a", 3), _req("b", 3), _req("c", 8)]
    admitted, deferred = batcher.fill(reqs)
    # a,b at width 3 cost 6; adding c widens to 8 -> 8*3=24 > 12 -> defer c
    assert [r.request_id for r in admitted] == ["a", "b"]
    assert [r.request_id for r in deferred] == ["c"]
