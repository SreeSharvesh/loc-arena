"""A dependency-aware, priority-ordered job queue.

The queue admits jobs, assigns each a stable id, and reports which are *runnable*: a job is runnable only once
every job in its ``depends_on`` set has succeeded. Dependencies reference the ids of jobs already submitted to
the queue (the model :meth:`~meridian_common.jobclient.client.JobClient.submit_graph` uses); a batch that
references its members by name is admitted through :meth:`submit_graph`, which orders it and rejects cycles.

Ownership: this module owns pending admission and dependency readiness. It is told when a job reaches a
terminal state so it can release the job's dependents. The ready-set ordering is deterministic — higher
priority first, then submission order — so scheduling is reproducible from ``(config, seed)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.errors import DependencyError


@dataclass(frozen=True)
class QueuedJob:
    """A pending job in the queue: its assigned id, its spec, and its submission sequence number."""

    job_id: str
    spec: JobSpec
    seq: int

    @property
    def priority(self) -> int:
        """The job's priority (higher runs first)."""
        return self.spec.priority


class JobQueue:
    """A priority queue over pending jobs whose readiness is gated on dependency success."""

    def __init__(self, *, prefix: str = "job") -> None:
        """Create an empty queue that mints ids as ``<prefix>-<n>`` in submission order."""
        self._prefix = prefix
        self._seq = 0
        self._pending: dict[str, QueuedJob] = {}
        self._known: set[str] = set()
        self._succeeded: set[str] = set()
        self._failed: set[str] = set()

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._prefix}-{self._seq}"

    def submit(self, spec: JobSpec) -> str:
        """Admit ``spec`` and return its job id; every dependency must already be a known job id.

        Raises:
            DependencyError: if a dependency id has not been submitted to this queue.
        """
        for dep in spec.depends_on:
            if dep not in self._known:
                raise DependencyError(f"unknown dependency {dep!r} for job {spec.name!r}", path=dep)
        job_id = self._next_id()
        self._pending[job_id] = QueuedJob(job_id=job_id, spec=spec, seq=self._seq)
        self._known.add(job_id)
        return job_id

    def submit_graph(self, specs: list[JobSpec]) -> dict[str, str]:
        """Admit a batch that references its members by :attr:`JobSpec.name`, in dependency order.

        The batch is topologically ordered so each job is admitted after its dependencies; a dependency
        cycle or a reference to a name outside the batch raises :class:`DependencyError`. Returns a mapping
        from each spec's name to its assigned job id.
        """
        by_name = {s.name: s for s in specs}
        if len(by_name) != len(specs):
            raise DependencyError("duplicate job names in graph", path="name")
        ordered = self._topo_order(specs, by_name)
        ids: dict[str, str] = {}
        for spec in ordered:
            resolved = tuple(ids[dep] for dep in spec.depends_on)
            admitted = JobSpec(
                name=spec.name,
                command=list(spec.command),
                cpu=spec.cpu,
                memory_mb=spec.memory_mb,
                priority=spec.priority,
                depends_on=resolved,
                labels=dict(spec.labels),
            )
            ids[spec.name] = self.submit(admitted)
        return ids

    @staticmethod
    def _topo_order(specs: list[JobSpec], by_name: dict[str, JobSpec]) -> list[JobSpec]:
        ordered: list[JobSpec] = []
        state: dict[str, int] = {}  # 0=unseen, 1=visiting, 2=done

        def visit(name: str) -> None:
            if state.get(name) == 2:
                return
            if state.get(name) == 1:
                raise DependencyError(f"dependency cycle at job {name!r}", path=name)
            if name not in by_name:
                raise DependencyError(f"unknown dependency {name!r}", path=name)
            state[name] = 1
            for dep in by_name[name].depends_on:
                visit(dep)
            state[name] = 2
            ordered.append(by_name[name])

        for spec in specs:
            visit(spec.name)
        return ordered

    def ready(self) -> list[QueuedJob]:
        """The runnable pending jobs (all dependencies succeeded), highest priority then earliest first."""
        runnable = [
            job
            for job in self._pending.values()
            if all(dep in self._succeeded for dep in job.spec.depends_on)
        ]
        runnable.sort(key=lambda job: (-job.priority, job.seq))
        return runnable

    def blocked(self) -> list[QueuedJob]:
        """Pending jobs that can never run because one of their dependencies failed or was cancelled."""
        return [
            job for job in self._pending.values() if any(dep in self._failed for dep in job.spec.depends_on)
        ]

    def dispatch(self, job_id: str) -> QueuedJob:
        """Remove ``job_id`` from the pending pool (it has been placed) and return its record."""
        return self._pending.pop(job_id)

    def notify_succeeded(self, job_id: str) -> None:
        """Record that ``job_id`` succeeded so its dependents may become runnable."""
        self._succeeded.add(job_id)

    def notify_failed(self, job_id: str) -> None:
        """Record that ``job_id`` failed or was cancelled so its dependents stay blocked."""
        self._failed.add(job_id)

    @property
    def pending_count(self) -> int:
        """The number of jobs still waiting to be placed."""
        return len(self._pending)

    def is_pending(self, job_id: str) -> bool:
        """Whether ``job_id`` is still waiting in the queue."""
        return job_id in self._pending

    def __contains__(self, job_id: str) -> bool:
        """Whether ``job_id`` has ever been admitted to this queue."""
        return job_id in self._known
