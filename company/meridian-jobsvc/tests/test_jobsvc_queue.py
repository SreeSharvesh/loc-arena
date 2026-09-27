from __future__ import annotations

import pytest

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.errors import DependencyError
from meridian_jobsvc.queue import JobQueue


def _spec(name: str, *, priority: int = 0, depends_on: tuple[str, ...] = ()) -> JobSpec:
    return JobSpec(name=name, command=["run", name], priority=priority, depends_on=depends_on)


def test_submit_mints_sequential_ids() -> None:
    q = JobQueue()
    assert q.submit(_spec("a")) == "job-1"
    assert q.submit(_spec("b")) == "job-2"
    assert q.pending_count == 2


def test_ready_orders_by_priority_then_sequence() -> None:
    q = JobQueue()
    q.submit(_spec("low", priority=1))
    q.submit(_spec("high", priority=5))
    q.submit(_spec("high2", priority=5))
    ready = q.ready()
    assert [job.spec.name for job in ready] == ["high", "high2", "low"]


def test_dependency_gates_readiness() -> None:
    q = JobQueue()
    prep = q.submit(_spec("prep"))
    child = q.submit(_spec("child", depends_on=(prep,)))
    assert [job.job_id for job in q.ready()] == [prep]
    q.dispatch(prep)
    q.notify_succeeded(prep)
    assert [job.job_id for job in q.ready()] == [child]


def test_unknown_dependency_rejected() -> None:
    q = JobQueue()
    with pytest.raises(DependencyError):
        q.submit(_spec("child", depends_on=("job-99",)))


def test_blocked_when_dependency_fails() -> None:
    q = JobQueue()
    prep = q.submit(_spec("prep"))
    child = q.submit(_spec("child", depends_on=(prep,)))
    q.dispatch(prep)
    q.notify_failed(prep)
    assert [job.job_id for job in q.blocked()] == [child]
    assert q.ready() == []


def test_submit_graph_orders_and_resolves_names() -> None:
    q = JobQueue()
    ids = q.submit_graph(
        [
            _spec("train", depends_on=("prep",)),
            _spec("prep"),
        ],
    )
    assert set(ids) == {"train", "prep"}
    # train depends on prep's assigned id, not its name.
    train_entry = q.dispatch(ids["train"])
    assert train_entry.spec.depends_on == (ids["prep"],)


def test_submit_graph_detects_cycle() -> None:
    q = JobQueue()
    with pytest.raises(DependencyError):
        q.submit_graph([_spec("a", depends_on=("b",)), _spec("b", depends_on=("a",))])


def test_submit_graph_rejects_reference_outside_batch() -> None:
    q = JobQueue()
    with pytest.raises(DependencyError):
        q.submit_graph([_spec("a", depends_on=("missing",))])


def test_membership_and_pending_tracking() -> None:
    q = JobQueue()
    jid = q.submit(_spec("a"))
    assert jid in q
    assert q.is_pending(jid)
    q.dispatch(jid)
    assert jid in q
    assert not q.is_pending(jid)
