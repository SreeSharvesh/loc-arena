from __future__ import annotations

from meridian_serving.queue import DeficitRoundRobin, TokenBucketLimiter
from meridian_serving.types import Request


def _req(rid: str, total: int) -> Request:
    # total_len = prompt_len + max_tokens; pick prompt so total matches
    return Request(rid, tuple(range(total - 2)), max_tokens=2)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_deficit_round_robin_is_fair_across_tenants() -> None:
    drr = DeficitRoundRobin(quantum=10)
    for i in range(3):
        drr.enqueue("noisy", _req(f"n{i}", 8))
    drr.enqueue("quiet", _req("q0", 8))
    dispatched = drr.dispatch_round()
    ids = [r.request_id for r in dispatched]
    # both tenants get service in the first round despite the noisy tenant's backlog
    assert "q0" in ids and any(i.startswith("n") for i in ids)


def test_deficit_round_robin_drains_all() -> None:
    drr = DeficitRoundRobin(quantum=8)
    for i in range(5):
        drr.enqueue("t", _req(f"r{i}", 8))
    out = drr.drain()
    assert len(out) == 5 and drr.pending() == 0


def test_token_bucket_allows_within_burst_then_blocks() -> None:
    clk = _Clock()
    tb = TokenBucketLimiter(refill_per_second=1.0, burst=3.0, clock=clk)
    assert tb.allow("t", 1) and tb.allow("t", 1) and tb.allow("t", 1)
    assert not tb.allow("t", 1)  # burst exhausted


def test_token_bucket_refills_over_time() -> None:
    clk = _Clock()
    tb = TokenBucketLimiter(refill_per_second=2.0, burst=2.0, clock=clk)
    assert tb.allow("t", 2)  # empties the bucket
    assert not tb.allow("t", 1)
    clk.t = 1.0  # 1 second -> +2 tokens (capped at burst 2)
    assert tb.allow("t", 2)


def test_token_bucket_is_per_tenant() -> None:
    tb = TokenBucketLimiter(refill_per_second=1.0, burst=1.0, clock=_Clock())
    assert tb.allow("a", 1) and tb.allow("b", 1)  # separate buckets
    assert not tb.allow("a", 1)
