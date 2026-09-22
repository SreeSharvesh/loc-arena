from __future__ import annotations

from meridian_serving.api import ServingEngine
from meridian_serving.queue import AdmissionLimits
from meridian_serving.types import Priority, Request


def _workload(n: int) -> list[Request]:
    return [
        Request(f"r{i}", tuple(range(i % 6 + 1)), max_tokens=3, priority=Priority.NORMAL) for i in range(n)
    ]


def test_serve_is_deterministic() -> None:
    reqs = _workload(24)
    a = ServingEngine(batch_size=4, cache_capacity=64).serve(reqs)
    b = ServingEngine(batch_size=4, cache_capacity=64).serve(reqs)
    assert a.served_checksum == b.served_checksum
    assert a.schedule_cost == b.schedule_cost
    assert tuple(r.tokens for r in a.responses) == tuple(r.tokens for r in b.responses)


def test_serve_produces_responses_for_admitted() -> None:
    reqs = _workload(10)
    res = ServingEngine().serve(reqs)
    assert res.admitted == 10 and len(res.responses) == 10
    assert all(r.generated_len == 3 for r in res.responses)


def test_schedule_cost_reflects_padding() -> None:
    reqs = _workload(8)
    res = ServingEngine(batch_size=4).serve(reqs)
    # cost is padded tokens: 8 requests padded to the global max prompt length (6)
    assert res.schedule_cost == 8 * 6


def test_admission_rejects_oversized() -> None:
    engine = ServingEngine(limits=AdmissionLimits(max_prompt_len=3, max_total_len=100))
    reqs = [Request("ok", (1, 2), max_tokens=2), Request("big", tuple(range(10)), max_tokens=2)]
    res = engine.serve(reqs)
    assert res.admitted == 1 and res.rejected == 1


def test_smaller_cache_causes_evictions() -> None:
    reqs = _workload(60)
    res = ServingEngine(cache_capacity=4, cache_key_space=40).serve(reqs)
    assert res.cache_evictions > 0  # a small cache over a wide key space evicts


def test_total_generated_tokens() -> None:
    res = ServingEngine().serve(_workload(5))
    assert res.total_generated == 5 * 3
