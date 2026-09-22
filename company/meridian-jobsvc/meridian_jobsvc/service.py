"""The job service: a :class:`~meridian_common.jobclient.models.JobTransport` over the cluster.

``JobService`` is the public face of the cluster. It implements the transport protocol a
:class:`~meridian_common.jobclient.client.JobClient` calls — ``submit`` / ``status`` / ``logs`` / ``cancel``,
each returning the mapping shapes the client expects — and backs it with the queue (dependency-aware
admission), the scheduler (capacity placement), and the worker runtime (long-lived stepping). The world
advances one deterministic step per ``status`` poll, so a client that submits a job and waits on it drives the
job to a terminal state without threads or a wall clock. distill and evalkit submit
their eval and distillation jobs through common's ``JobClient`` against this service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meridian_common.jobclient.models import JobSpec, JobState
from meridian_jobsvc.accountant.ledger import Ledger
from meridian_jobsvc.errors import DrainError, JobNotFound, QuotaExceeded, SchedulingError
from meridian_jobsvc.lifecycle.nodes import NodeLifecycle
from meridian_jobsvc.queue.jobqueue import JobQueue
from meridian_jobsvc.scheduler.placement import Scheduler
from meridian_jobsvc.types import Checkpoint, Node
from meridian_jobsvc.worker.runtime import WorkerRuntime

_TERMINAL = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


@dataclass
class _JobEntry:
    job_id: str
    spec: JobSpec
    total_steps: int
    identity: str
    state: JobState = JobState.PENDING
    exit_code: int | None = None
    message: str = ""
    progress: float = 0.0
    logs: list[str] = field(default_factory=list)


def _default_nodes(count: int, per_node_cpu: float, per_node_mem: int) -> list[Node]:
    return [
        Node(node_id=f"node-{i}", cpu_capacity=per_node_cpu, memory_mb=per_node_mem) for i in range(count)
    ]


class JobService:
    """A cluster-backed job transport: submit, poll, and cancel jobs; drain and rebalance nodes."""

    def __init__(
        self,
        nodes: list[Node] | None = None,
        *,
        clock: Callable[[], int] | None = None,
        default_steps: int = 3,
        node_count: int = 4,
        per_node_cpu: float = 4.0,
        per_node_mem: int = 4096,
    ) -> None:
        """Wire the queue, scheduler, worker, lifecycle, and ledger over a node pool."""
        pool = nodes if nodes is not None else _default_nodes(node_count, per_node_cpu, per_node_mem)
        self._queue = JobQueue()
        self._scheduler = Scheduler(pool)
        self._worker = WorkerRuntime(clock=clock)
        self._lifecycle = NodeLifecycle(self._scheduler, self._worker)
        self._ledger = Ledger()
        self._default_steps = default_steps
        self._entries: dict[str, _JobEntry] = {}

    # -- transport protocol ------------------------------------------------

    def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Admit a job spec mapping and return ``{"job_id": ...}`` (the job starts pending)."""
        job_spec = _spec_from_mapping(spec)
        job_id = self._queue.submit(job_spec)
        self._entries[job_id] = _JobEntry(
            job_id=job_id,
            spec=job_spec,
            total_steps=_steps_of(job_spec, self._default_steps),
            identity=str(job_spec.labels.get("identity", "anonymous")),
        )
        return {"job_id": job_id}

    def status(self, job_id: str) -> dict[str, Any]:
        """Advance the cluster one step, then return ``job_id``'s status mapping."""
        self.tick()
        return self._status_of(job_id)

    def logs(self, job_id: str) -> list[str]:
        """Return ``job_id``'s log lines."""
        return list(self._entry(job_id).logs)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel ``job_id`` if it is not already terminal and return the resulting status mapping."""
        entry = self._entry(job_id)
        if entry.state not in _TERMINAL:
            if self._worker.is_running(job_id):
                self._worker.release(job_id)
            self._scheduler.release(job_id)
            if self._queue.is_pending(job_id):
                self._queue.dispatch(job_id)
            self._queue.notify_failed(job_id)
            entry.state = JobState.CANCELLED
            entry.message = "cancelled"
            entry.logs.append(f"{job_id} cancelled")
        return self._status_of(job_id)

    # -- cluster operations ------------------------------------------------

    def tick(self) -> None:
        """Advance the cluster one deterministic step: place ready jobs, fail blocked jobs, step running."""
        self._place_ready()
        self._fail_blocked()
        self._step_running()

    def run_to_idle(self, *, max_ticks: int = 1000) -> None:
        """Tick until no job is pending or running, or ``max_ticks`` is reached."""
        for _ in range(max_ticks):
            if self._queue.pending_count == 0 and not self._any_running():
                return
            self.tick()

    def drain_node(self, node_id: str) -> list[str]:
        """Drain ``node_id`` and re-place each evicted job on another node, resuming from its checkpoint.

        Returns the ids of the jobs that were moved.

        Raises:
            DrainError: if no node has room to resume an evicted job.
        """
        moved: list[str] = []
        for drained in self._lifecycle.drain(node_id):
            entry = self._entry(drained.job_id)
            target = self._scheduler.assign(drained.job_id, entry.spec)
            if target is None:
                raise DrainError(
                    f"no capacity to resume {drained.job_id!r} after draining {node_id!r}", path=node_id
                )
            self._worker.resume(drained.job_id, target, entry.total_steps, drained.checkpoint)
            entry.logs.append(f"{drained.job_id} resumed on {target} from step {drained.checkpoint.step}")
            moved.append(drained.job_id)
        return moved

    def job_checkpoint(self, job_id: str) -> Checkpoint:
        """The current checkpoint for ``job_id`` (raises if it is not running or finished)."""
        return self._worker.checkpoint(job_id)

    @property
    def queue(self) -> JobQueue:
        """The admission queue."""
        return self._queue

    @property
    def scheduler(self) -> Scheduler:
        """The placement scheduler."""
        return self._scheduler

    @property
    def worker(self) -> WorkerRuntime:
        """The worker runtime."""
        return self._worker

    @property
    def lifecycle(self) -> NodeLifecycle:
        """The node lifecycle controller."""
        return self._lifecycle

    @property
    def ledger(self) -> Ledger:
        """The spend/quota ledger."""
        return self._ledger

    # -- internals ---------------------------------------------------------

    def _entry(self, job_id: str) -> _JobEntry:
        entry = self._entries.get(job_id)
        if entry is None:
            raise JobNotFound(f"unknown job {job_id!r}", path=job_id)
        return entry

    def _status_of(self, job_id: str) -> dict[str, Any]:
        entry = self._entry(job_id)
        return {
            "job_id": job_id,
            "state": entry.state.value,
            "exit_code": entry.exit_code,
            "progress": entry.progress,
            "message": entry.message,
        }

    def _any_running(self) -> bool:
        return any(entry.state is JobState.RUNNING for entry in self._entries.values())

    def _place_ready(self) -> None:
        for queued in self._queue.ready():
            entry = self._entries[queued.job_id]
            try:
                node_id = self._scheduler.assign(queued.job_id, queued.spec)
            except SchedulingError as exc:
                self._queue.dispatch(queued.job_id)
                self._queue.notify_failed(queued.job_id)
                entry.state = JobState.FAILED
                entry.exit_code = 2
                entry.message = str(exc)
                entry.logs.append(f"{queued.job_id} failed to schedule: {exc.message}")
                continue
            if node_id is None:
                continue
            self._queue.dispatch(queued.job_id)
            self._worker.start(queued.job_id, node_id, entry.total_steps)
            entry.state = JobState.RUNNING
            entry.logs.append(f"{queued.job_id} started on {node_id}")

    def _fail_blocked(self) -> None:
        for queued in self._queue.blocked():
            entry = self._entries[queued.job_id]
            self._queue.dispatch(queued.job_id)
            self._queue.notify_failed(queued.job_id)
            entry.state = JobState.FAILED
            entry.exit_code = 1
            entry.message = "dependency failed"
            entry.logs.append(f"{queued.job_id} blocked by a failed dependency")

    def _step_running(self) -> None:
        for job_id in sorted(self._entries):
            entry = self._entries[job_id]
            if entry.state is not JobState.RUNNING or not self._worker.is_running(job_id):
                continue
            self._worker.step(job_id)
            entry.progress = self._worker.progress(job_id)
            if self._worker.is_finished(job_id):
                self._finish(entry)

    def _finish(self, entry: _JobEntry) -> None:
        self._scheduler.release(entry.job_id)
        self._queue.notify_succeeded(entry.job_id)
        entry.state = JobState.SUCCEEDED
        entry.exit_code = 0
        entry.progress = 1.0
        entry.logs.append(f"{entry.job_id} succeeded")
        try:
            self._ledger.charge(
                entry.identity,
                compute=entry.spec.cpu * entry.total_steps,
                inference=float(entry.spec.labels.get("inference", 0.0)),
            )
        except QuotaExceeded as exc:
            entry.message = str(exc)


def _spec_from_mapping(spec: dict[str, Any]) -> JobSpec:
    return JobSpec(
        name=str(spec["name"]),
        command=[str(arg) for arg in spec.get("command", [])],
        cpu=float(spec.get("cpu", 1.0)),
        memory_mb=int(spec.get("memory_mb", 512)),
        priority=int(spec.get("priority", 0)),
        depends_on=tuple(str(dep) for dep in spec.get("depends_on", ())),
        labels={str(k): str(v) for k, v in dict(spec.get("labels", {})).items()},
    )


def _steps_of(spec: JobSpec, default_steps: int) -> int:
    raw = spec.labels.get("steps")
    if raw is None:
        return max(default_steps, 1)
    try:
        return max(int(raw), 1)
    except ValueError:
        return max(default_steps, 1)
