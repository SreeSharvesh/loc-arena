from __future__ import annotations

from typing import Any

from meridian_common.errors import TransportError
from meridian_common.jobclient import JobClient, JobSpec, JobState
from meridian_common.retry import FixedBackoff


class FakeJobsvc:
    def __init__(self) -> None:
        self._status: dict[str, dict[str, Any]] = {}
        self._n = 0
        self.fail_next = 0

    def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransportError("submit flaky")
        self._n += 1
        jid = f"job-{self._n}"
        self._status[jid] = {"job_id": jid, "state": "running", "progress": 0.0}
        return {"job_id": jid}

    def status(self, job_id: str) -> dict[str, Any]:
        return dict(self._status[job_id])

    def logs(self, job_id: str) -> list[str]:
        return [f"line for {job_id}"]

    def cancel(self, job_id: str) -> dict[str, Any]:
        self._status[job_id]["state"] = "cancelled"
        return dict(self._status[job_id])

    def finish(self, job_id: str) -> None:
        self._status[job_id]["state"] = "succeeded"
        self._status[job_id]["exit_code"] = 0


def _client(svc: FakeJobsvc) -> JobClient:
    return JobClient(svc, max_attempts=4, backoff=FixedBackoff(0.0), sleep=lambda _s: None)


def test_submit_and_typed_status() -> None:
    svc = FakeJobsvc()
    client = _client(svc)
    jid = client.submit(JobSpec(name="eval", command=["run"], priority=3))
    st = client.status(jid)
    assert st.job_id == jid and st.state is JobState.RUNNING and not st.terminal


def test_submit_retries_transient_failure() -> None:
    svc = FakeJobsvc()
    svc.fail_next = 2
    jid = _client(svc).submit(JobSpec(name="x", command=["c"]))
    assert jid == "job-1"


def test_wait_polls_until_terminal() -> None:
    svc = FakeJobsvc()
    client = _client(svc)
    jid = client.submit(JobSpec(name="x", command=["c"]))
    svc.finish(jid)
    final = client.wait(jid, poll_interval=0.0, max_polls=5)
    assert final.state is JobState.SUCCEEDED and final.terminal and final.exit_code == 0


def test_cancel_and_logs() -> None:
    svc = FakeJobsvc()
    client = _client(svc)
    jid = client.submit(JobSpec(name="x", command=["c"]))
    assert client.cancel(jid).state is JobState.CANCELLED
    assert client.logs(jid) == [f"line for {jid}"]


def test_spec_serialization_roundtrips_fields() -> None:
    spec = JobSpec(name="x", command=["a", "b"], cpu=2.0, memory_mb=1024, depends_on=("job-0",))
    d = spec.to_dict()
    assert d["command"] == ["a", "b"] and d["depends_on"] == ["job-0"] and d["cpu"] == 2.0
