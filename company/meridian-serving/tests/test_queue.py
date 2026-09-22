from __future__ import annotations

import pytest

from meridian_serving.errors import QueueFullError
from meridian_serving.queue import AdmissionController, AdmissionLimits, RequestQueue
from meridian_serving.types import Priority, Request


def _req(rid: str, prompt_len: int = 4, priority: Priority = Priority.NORMAL) -> Request:
    return Request(rid, tuple(range(prompt_len)), max_tokens=8, priority=priority)


def test_priority_then_fifo_order() -> None:
    q = RequestQueue()
    q.push(_req("a"))
    q.push(_req("b", priority=Priority.HIGH))
    q.push(_req("c"))
    q.push(_req("d", priority=Priority.HIGH))
    order = [q.pop().request_id for _ in range(4)]
    assert order == ["b", "d", "a", "c"]  # HIGH before NORMAL; FIFO within a priority


def test_peek_does_not_remove() -> None:
    q = RequestQueue()
    q.push(_req("a"))
    assert q.peek().request_id == "a"
    assert len(q) == 1


def test_capacity_and_full() -> None:
    q = RequestQueue(capacity=2)
    q.push(_req("a"))
    q.push(_req("b"))
    assert q.is_full()
    with pytest.raises(QueueFullError):
        q.push(_req("c"))


def test_drain_and_snapshot() -> None:
    q = RequestQueue()
    for i in range(5):
        q.push(_req(f"r{i}"))
    snap = [r.request_id for r in q.snapshot()]
    drained = [r.request_id for r in q.drain(3)]
    assert drained == snap[:3] and len(q) == 2


def test_pop_empty_raises() -> None:
    with pytest.raises(IndexError):
        RequestQueue().pop()


def test_counts_by_priority() -> None:
    q = RequestQueue()
    q.push(_req("a", priority=Priority.LOW))
    q.push(_req("b", priority=Priority.HIGH))
    q.push(_req("c", priority=Priority.HIGH))
    counts = q.counts_by_priority()
    assert counts[Priority.HIGH] == 2 and counts[Priority.LOW] == 1 and counts[Priority.NORMAL] == 0


def test_admission_limits() -> None:
    ctrl = AdmissionController(AdmissionLimits(max_prompt_len=8, max_total_len=16, max_inflight_tokens=40))
    assert ctrl.admit(_req("ok", prompt_len=4)).admitted
    assert ctrl.evaluate(Request("long", tuple(range(9)), max_tokens=1)).reason == "prompt_too_long"
    assert ctrl.evaluate(Request("seq", tuple(range(4)), max_tokens=20)).reason == "sequence_too_long"


def test_admission_inflight_budget_and_retire() -> None:
    ctrl = AdmissionController(AdmissionLimits(max_inflight_tokens=24, max_total_len=100))
    a = Request("a", tuple(range(4)), max_tokens=8)  # total 12
    b = Request("b", tuple(range(4)), max_tokens=8)  # total 12
    c = Request("c", tuple(range(4)), max_tokens=8)
    assert ctrl.admit(a).admitted and ctrl.admit(b).admitted
    assert not ctrl.admit(c).admitted  # 24 + 12 > 24
    ctrl.retire(a)
    assert ctrl.admit(c).admitted  # budget freed
