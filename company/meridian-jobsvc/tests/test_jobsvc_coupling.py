"""End-to-end coupling: drive the real meridian-common JobClient against JobService.

This is the load-bearing cross-repo contract: distill and evalkit submit jobs through
``meridian_common.jobclient.JobClient``, which talks to whatever implements ``JobTransport``. ``JobService``
implements it, so the client must round-trip submit -> status -> wait -> terminal against a real service.
"""

from __future__ import annotations

from meridian_common.jobclient import JobClient, JobSpec, JobState
from meridian_common.retry import FixedBackoff
from meridian_jobsvc.service import JobService


def _client(svc: JobService) -> JobClient:
    return JobClient(svc, backoff=FixedBackoff(0.0), sleep=lambda _s: None)


def test_client_submits_and_waits_to_terminal() -> None:
    client = _client(JobService())
    jid = client.submit(JobSpec(name="eval", command=["run"], labels={"steps": "2"}))
    final = client.wait(jid, poll_interval=0.0, max_polls=50)
    assert final.job_id == jid
    assert final.state is JobState.SUCCEEDED
    assert final.terminal
    assert final.exit_code == 0


def test_client_submit_graph_respects_dependencies() -> None:
    client = _client(JobService())
    ids = client.submit_graph(
        [
            JobSpec(name="prep", command=["p"], labels={"steps": "2"}),
            JobSpec(name="train", command=["t"], depends_on=("prep",), labels={"steps": "2"}),
        ],
    )
    train = client.wait(ids["train"], poll_interval=0.0, max_polls=100)
    assert train.state is JobState.SUCCEEDED
    assert client.status(ids["prep"]).state is JobState.SUCCEEDED


def test_client_cancel_and_logs() -> None:
    svc = JobService()
    client = _client(svc)
    jid = client.submit(JobSpec(name="x", command=["c"], labels={"steps": "10"}))
    client.status(jid)  # advance so it is running
    cancelled = client.cancel(jid)
    assert cancelled.state is JobState.CANCELLED
    assert client.logs(jid)  # non-empty log lines


def test_client_status_reports_progress() -> None:
    client = _client(JobService())
    jid = client.submit(JobSpec(name="x", command=["c"], labels={"steps": "4"}))
    first = client.status(jid)
    assert first.state is JobState.RUNNING
    assert 0.0 < first.progress < 1.0
