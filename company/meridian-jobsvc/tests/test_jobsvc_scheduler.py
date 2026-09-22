from __future__ import annotations

import pytest

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.errors import SchedulingError
from meridian_jobsvc.scheduler import Autoscaler, ScalePolicy, Scheduler
from meridian_jobsvc.types import Node


def _spec(name: str, *, cpu: float = 1.0, memory_mb: int = 512) -> JobSpec:
    return JobSpec(name=name, command=["run"], cpu=cpu, memory_mb=memory_mb)


# -- placement ---------------------------------------------------------------


def test_assign_places_first_eligible_node() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=2), Node("n1", cpu_capacity=2)])
    assert sched.assign("a", _spec("a", cpu=1)) == "n0"
    assert sched.assign("b", _spec("b", cpu=1)) == "n0"
    # n0 now full for a 2-cpu ask; spills to n1.
    assert sched.assign("c", _spec("c", cpu=2)) == "n1"


def test_assign_returns_none_when_full() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=1)])
    assert sched.assign("a", _spec("a", cpu=1)) == "n0"
    assert sched.assign("b", _spec("b", cpu=1)) is None


def test_release_frees_capacity() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=1)])
    sched.assign("a", _spec("a", cpu=1))
    sched.release("a")
    assert sched.assign("b", _spec("b", cpu=1)) == "n0"


def test_impossible_ask_raises() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=2)])
    with pytest.raises(SchedulingError):
        sched.assign("a", _spec("a", cpu=8))


def test_cordoned_node_is_skipped() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=4), Node("n1", cpu_capacity=4)])
    sched.node("n0").cordoned = True
    assert sched.assign("a", _spec("a", cpu=1)) == "n1"


def test_utilization_tracks_reservations() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=4)])
    assert sched.utilization() == 0.0
    sched.assign("a", _spec("a", cpu=2))
    assert sched.utilization() == 0.5
    assert sched.node_of("a") == "n0"


def test_memory_capacity_is_enforced() -> None:
    sched = Scheduler([Node("n0", cpu_capacity=10, memory_mb=1000)])
    assert sched.assign("a", _spec("a", cpu=1, memory_mb=800)) == "n0"
    assert sched.assign("b", _spec("b", cpu=1, memory_mb=800)) is None


# -- autoscaler --------------------------------------------------------------


def test_autoscaler_scales_up_over_band() -> None:
    a = Autoscaler(ScalePolicy(per_node_cpu=8.0, target_low=0.55, target_high=0.85))
    assert a.desired(1, 40.0) == 2


def test_autoscaler_scales_down_under_band() -> None:
    a = Autoscaler(ScalePolicy(per_node_cpu=8.0, target_low=0.55, target_high=0.85))
    assert a.desired(4, 2.0) == 3


def test_autoscaler_holds_inside_band() -> None:
    a = Autoscaler(ScalePolicy(per_node_cpu=8.0, target_low=0.55, target_high=0.85))
    assert a.desired(1, 6.0) == 1  # util 0.75, inside band


@pytest.mark.parametrize("demand", [6.0, 9.0, 10.0, 12.0, 20.0])
def test_autoscaler_settles_for_typical_loads(demand: float) -> None:
    a = Autoscaler()
    assert a.settles(demand, initial_nodes=1, steps=16)


def test_autoscaler_respects_bounds() -> None:
    a = Autoscaler(ScalePolicy(per_node_cpu=8.0, min_nodes=2, max_nodes=3))
    assert a.desired(2, 0.0) == 2  # cannot shrink below min
    assert a.desired(3, 1000.0) == 3  # cannot grow past max


def test_autoscaler_simulate_records_trajectory() -> None:
    a = Autoscaler()
    traj = a.simulate(10.0, initial_nodes=1, steps=3)
    assert traj[0] == 1
    assert len(traj) == 4
