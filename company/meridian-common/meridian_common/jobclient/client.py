"""A typed, retrying client to the job service.

:class:`JobClient` wraps a :class:`~meridian_common.jobclient.models.JobTransport` with retries (backoff +
circuit breaker + deadline) and returns typed :class:`~meridian_common.jobclient.models.JobStatus` objects. It
translates transport-level failures into the Meridian error hierarchy so callers handle them uniformly.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from meridian_common.errors import TransportError, ValidationError
from meridian_common.jobclient.models import JobSpec, JobState, JobStatus, JobTransport
from meridian_common.retry.backoff import BackoffPolicy, ExponentialBackoff
from meridian_common.retry.circuit import CircuitBreaker, retry_call
from meridian_common.retry.deadline import Deadline


def _parse_status(data: dict[str, object]) -> JobStatus:
    try:
        state = JobState(str(data["state"]))
    except (KeyError, ValueError) as exc:
        raise ValidationError(f"bad job status payload: {data!r}", path="state") from exc
    exit_code = data.get("exit_code")
    return JobStatus(
        job_id=str(data["job_id"]),
        state=state,
        exit_code=int(exit_code) if isinstance(exit_code, int) else None,
        progress=float(data.get("progress", 0.0)),  # type: ignore[arg-type]
        message=str(data.get("message", "")),
    )


class JobClient:
    """A retrying, typed facade over a :class:`JobTransport`."""

    def __init__(
        self,
        transport: JobTransport,
        *,
        max_attempts: int = 3,
        backoff: BackoffPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the client to its transport and its retry policy (backoff, breaker, sleep)."""
        self._transport = transport
        self._max_attempts = max_attempts
        self._backoff = backoff if backoff is not None else ExponentialBackoff(base=0.05, cap=2.0)
        self._breaker = breaker
        self._sleep = sleep

    def _call(self, fn: Callable[[], object], deadline: Deadline | None) -> object:
        return retry_call(
            fn,
            max_attempts=self._max_attempts,
            backoff=self._backoff,
            deadline=deadline,
            breaker=self._breaker,
            sleep=self._sleep,
            retry_on=(TransportError,),
        )

    def submit(self, spec: JobSpec, *, deadline: Deadline | None = None) -> str:
        """Submit ``spec`` and return the job id (retried under the client's policy)."""
        result = self._call(lambda: self._transport.submit(spec.to_dict()), deadline)
        if not isinstance(result, dict) or "job_id" not in result:
            raise ValidationError(f"submit returned no job_id: {result!r}", path="job_id")
        return str(result["job_id"])

    def status(self, job_id: str, *, deadline: Deadline | None = None) -> JobStatus:
        """Fetch the typed status of ``job_id`` (retried)."""
        result = self._call(lambda: self._transport.status(job_id), deadline)
        assert isinstance(result, dict)  # transport contract; retry_call surfaced any error
        return _parse_status(result)

    def logs(self, job_id: str, *, deadline: Deadline | None = None) -> list[str]:
        """Fetch the log lines of ``job_id`` (retried)."""
        result = self._call(lambda: self._transport.logs(job_id), deadline)
        if not isinstance(result, list):
            raise ValidationError("logs must be a list of lines", path="logs")
        return [str(line) for line in result]

    def cancel(self, job_id: str, *, deadline: Deadline | None = None) -> JobStatus:
        """Cancel ``job_id`` and return the resulting typed status (retried)."""
        result = self._call(lambda: self._transport.cancel(job_id), deadline)
        assert isinstance(result, dict)
        return _parse_status(result)

    def wait(
        self,
        job_id: str,
        *,
        poll_interval: float = 0.01,
        max_polls: int = 1000,
        deadline: Deadline | None = None,
    ) -> JobStatus:
        """Poll ``job_id`` until it reaches a terminal state or the poll/deadline budget is exhausted."""
        last = self.status(job_id, deadline=deadline)
        polls = 0
        while not last.terminal and polls < max_polls:
            if deadline is not None and deadline.expired():
                break
            self._sleep(poll_interval)
            last = self.status(job_id, deadline=deadline)
            polls += 1
        return last

    def submit_graph(self, specs: list[JobSpec], *, deadline: Deadline | None = None) -> dict[str, str]:
        """Submit a set of jobs in dependency order, rewriting ``depends_on`` names to submitted job ids.

        ``specs`` reference each other by ``JobSpec.name``; this topologically orders them (a cycle or a
        missing dependency raises :class:`ValidationError`) and submits each once its dependencies are
        submitted, returning ``{spec_name: job_id}``.
        """
        by_name = {s.name: s for s in specs}
        if len(by_name) != len(specs):
            raise ValidationError("duplicate job names in graph", path="name")
        ordered = _topo_order(specs, by_name)
        ids: dict[str, str] = {}
        for spec in ordered:
            resolved = tuple(ids[d] for d in spec.depends_on)
            submitted = JobSpec(
                name=spec.name,
                command=list(spec.command),
                cpu=spec.cpu,
                memory_mb=spec.memory_mb,
                priority=spec.priority,
                depends_on=resolved,
                labels=dict(spec.labels),
            )
            ids[spec.name] = self.submit(submitted, deadline=deadline)
        return ids


def _topo_order(specs: list[JobSpec], by_name: dict[str, JobSpec]) -> list[JobSpec]:
    ordered: list[JobSpec] = []
    state: dict[str, int] = {}  # 0=unseen, 1=visiting, 2=done

    def visit(name: str) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise ValidationError(f"dependency cycle at job {name!r}", path=name)
        if name not in by_name:
            raise ValidationError(f"unknown job dependency {name!r}", path=name)
        state[name] = 1
        for dep in by_name[name].depends_on:
            visit(dep)
        state[name] = 2
        ordered.append(by_name[name])

    for spec in specs:
        visit(spec.name)
    return ordered
