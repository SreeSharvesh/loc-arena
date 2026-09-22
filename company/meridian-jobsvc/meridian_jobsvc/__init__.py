"""meridian-jobsvc: the Meridian cluster and job-orchestration service.

Owns the dependency-aware priority job queue, the capacity-based job scheduler and its autoscaler simulation,
the worker runtime that hosts long-lived jobs (heartbeat and checkpoint), node lifecycle (cordon, drain,
rebalance), the spend/quota ledger, and a compact job-spec DSL. Its :class:`JobService` implements the
``meridian_common.jobclient.JobTransport`` protocol, so a ``JobClient`` submits and waits on jobs against it.
Depends only on meridian-common; runs the eval and distillation jobs that distill and evalkit submit.
"""

from __future__ import annotations

__version__ = "0.4.0"

from meridian_jobsvc.accountant import Ledger, Quota, Spend
from meridian_jobsvc.dsl import parse_spec
from meridian_jobsvc.errors import (
    DependencyError,
    DrainError,
    JobNotFound,
    QuotaExceeded,
    SchedulingError,
    SpecSyntaxError,
)
from meridian_jobsvc.lifecycle import NodeLifecycle
from meridian_jobsvc.queue import JobQueue, QueuedJob
from meridian_jobsvc.scheduler import Autoscaler, ScalePolicy, Scheduler
from meridian_jobsvc.service import JobService
from meridian_jobsvc.types import Checkpoint, DrainedJob, JobSpec, JobState, JobStatus, Node
from meridian_jobsvc.worker import WorkerRuntime

__all__ = [
    "Autoscaler",
    "Checkpoint",
    "DependencyError",
    "DrainError",
    "DrainedJob",
    "JobNotFound",
    "JobQueue",
    "JobService",
    "JobSpec",
    "JobState",
    "JobStatus",
    "Ledger",
    "Node",
    "NodeLifecycle",
    "Quota",
    "QueuedJob",
    "QuotaExceeded",
    "ScalePolicy",
    "Scheduler",
    "SchedulingError",
    "Spend",
    "SpecSyntaxError",
    "WorkerRuntime",
    "__version__",
    "parse_spec",
]
