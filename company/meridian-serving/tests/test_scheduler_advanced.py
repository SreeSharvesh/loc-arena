from __future__ import annotations

from meridian_serving.scheduler import (
    ChunkedPrefillPlanner,
    PreemptiveScheduler,
    RunningState,
    SpeculativeDecoder,
)
from meridian_serving.types import Priority, Request


def _req(rid: str, prompt_len: int, priority: Priority = Priority.NORMAL, seq: int = 0) -> Request:
    return Request(rid, tuple(range(prompt_len)), max_tokens=4, priority=priority, arrival_seq=seq)


def test_chunked_prefill_splits_long_prompt() -> None:
    planner = ChunkedPrefillPlanner(chunk_size=4)
    chunks = planner.chunks_for(_req("a", 10))
    assert [c.length for c in chunks] == [4, 4, 2]
    assert chunks[-1].is_last


def test_chunked_prefill_packs_a_step() -> None:
    planner = ChunkedPrefillPlanner(chunk_size=4, step_token_budget=8)
    pending = planner.chunks_for(_req("a", 12))  # 3 chunks of 4
    this_step, remaining = planner.plan_step(pending)
    assert sum(c.length for c in this_step) <= 8 and remaining


def test_preemptive_scheduler_admits_within_budget() -> None:
    sched = PreemptiveScheduler(memory_tokens=100)
    state = RunningState()
    decision = sched.schedule(state, [_req("a", 10), _req("b", 10)])
    assert len(decision.admitted) == 2 and not decision.preempted


def test_preemptive_scheduler_preempts_lower_priority() -> None:
    sched = PreemptiveScheduler(memory_tokens=20)  # total_len each = prompt+4
    state = RunningState()
    low = _req("low", 12, Priority.LOW, seq=0)  # total 16
    sched.schedule(state, [low])  # low now running (16 <= 20)
    high = _req("high", 12, Priority.HIGH, seq=1)  # total 16, needs room
    decision = sched.schedule(state, [high])
    assert "high" in [r.request_id for r in decision.admitted]
    assert "low" in [r.request_id for r in decision.preempted]


def test_preemptive_scheduler_resumes_swapped() -> None:
    sched = PreemptiveScheduler(memory_tokens=16)
    state = RunningState()
    state.swapped.append(_req("s", 8, Priority.NORMAL))  # total 12
    decision = sched.schedule(state, [])
    assert "s" in [r.request_id for r in decision.resumed]


def test_speculative_decode_accepts_matching_draft() -> None:
    # draft and target agree: token 1 is always best -> all proposals accepted, plus one target token
    def logits(context: tuple[int, ...]) -> list[float]:
        return [0.0, 9.0, 0.0]

    dec = SpeculativeDecoder(logits, logits, lookahead=3)
    result = dec.step((0,))
    assert result.accepted_len == 4  # 3 accepted + 1 target token
    out = dec.decode((0,), max_tokens=5)
    assert out == (1, 1, 1, 1, 1)


def test_speculative_decode_rejects_on_mismatch() -> None:
    def draft(context: tuple[int, ...]) -> list[float]:
        return [9.0, 0.0]  # draft prefers 0

    def target(context: tuple[int, ...]) -> list[float]:
        return [0.0, 9.0]  # target prefers 1

    dec = SpeculativeDecoder(draft, target, lookahead=3)
    result = dec.step((5,))
    assert result.accepted == (1,)  # first proposal (0) rejected, target correction (1) emitted
