"""Typed data models and the transport protocol for the jobsvc client.

These are the wire DTOs the job service and its clients agree on. ``meridian-common`` owns them because every
repo that submits jobs (distill, evalkit) depends on common, never on jobsvc directly; the concrete jobsvc
server implements the :class:`JobTransport` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class JobState(Enum):
    """The lifecycle states a job moves through."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobSpec:
    """A job submission: the command, resource ask, priority, and optional dependencies."""

    name: str
    command: list[str]
    cpu: float = 1.0
    memory_mb: int = 512
    priority: int = 0
    depends_on: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the spec to a JSON-friendly mapping."""
        return {
            "name": self.name,
            "command": list(self.command),
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class JobStatus:
    """A job's current status: id, state, and progress/exit metadata."""

    job_id: str
    state: JobState
    exit_code: int | None = None
    progress: float = 0.0
    message: str = ""

    @property
    def terminal(self) -> bool:
        """Whether the job has reached a terminal state."""
        return self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class JobTransport(Protocol):
    """The transport a :class:`~meridian_common.jobclient.client.JobClient` calls (jobsvc implements it)."""

    def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Submit a job spec; return ``{"job_id": ...}``."""
        ...

    def status(self, job_id: str) -> dict[str, Any]:
        """Return the job's status mapping."""
        ...

    def logs(self, job_id: str) -> list[str]:
        """Return the job's log lines."""
        ...

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel the job; return the resulting status mapping."""
        ...
