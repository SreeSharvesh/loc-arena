from __future__ import annotations

from meridian_common import cost


def test_record_accumulates_total_and_by_kind() -> None:
    cost.reset()
    cost.record("token", 3)
    cost.record("token", 2)
    cost.record("teacher")
    assert cost.total() == 6
    assert cost.snapshot() == {"token": 5, "teacher": 1}


def test_reset_zeros_the_meter() -> None:
    cost.record("x", 10)
    cost.reset()
    assert cost.total() == 0
    assert cost.snapshot() == {}


def test_snapshot_is_a_copy() -> None:
    cost.reset()
    cost.record("a", 1)
    snap = cost.snapshot()
    snap["a"] = 999
    assert cost.snapshot() == {"a": 1}


def test_state_is_not_reachable_as_a_module_global() -> None:
    # The running totals live in a closure; there is no module-level accumulator to read or poke.
    assert not hasattr(cost, "_work")
    assert not hasattr(cost, "work")
