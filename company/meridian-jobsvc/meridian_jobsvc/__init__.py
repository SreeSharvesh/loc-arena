"""meridian-jobsvc: the Meridian cluster and job-orchestration service.

Its :class:`JobService` keeps every job and gets it run as a process on a node of the cluster's fleet: the
dependency-aware priority job queue orders the waiting jobs, each starts on the first free node with its cpu
and memory while its team is within the cluster's caps, and the daemon on that node runs it (``fleet``), under
an identity of its own that the central authority issues it. A job can be held and released, a node drained,
whose job then runs again elsewhere, and a schedule submits a job on a cron expression or an interval
(``schedules``); each team's CPU-hours are charged from the wall time its jobs ran. It is served over HTTP and
as MCP tools (``api``), authorized by that central authority, which the deployment provides (``authority``),
starts from its own seed of teams (``seed``), and keeps what it holds in a database, so a process that starts
again holds it too (``state``). The package also holds the capacity-based scheduler and its
autoscaler simulation, the worker runtime that hosts long-lived jobs (heartbeat and checkpoint), node
lifecycle (cordon, drain, rebalance), the spend/quota ledger, and a compact job-spec DSL. Depends only on
meridian-common.
"""

from __future__ import annotations

__version__ = "0.8.0"

from meridian_jobsvc.accountant import Ledger, Quota, Spend
from meridian_jobsvc.authority import JobIdentity, WorkloadIssuer
from meridian_jobsvc.dsl import parse_spec
from meridian_jobsvc.errors import (
    CredentialRefused,
    CredentialUnavailable,
    DependencyError,
    DrainError,
    JobNotFound,
    JobStateConflict,
    NodeNotFound,
    NotJobOwner,
    PriorityError,
    QuotaExceeded,
    ScheduleNotFound,
    SchedulingError,
    SpecSyntaxError,
    SubmissionRefused,
)
from meridian_jobsvc.fleet import NodeDaemon, NodeUnavailable, ProcessOutput, ProcessState, ProcessStatus
from meridian_jobsvc.lifecycle import NodeLifecycle
from meridian_jobsvc.models import ClusterNode, Job, JobLogs, JobRequest, Jobs, Nodes, NodeState, Usage
from meridian_jobsvc.queue import JobQueue, QueuedJob
from meridian_jobsvc.scheduler import Autoscaler, ScalePolicy, Scheduler
from meridian_jobsvc.schedules import Schedule, ScheduleRequest, Schedules
from meridian_jobsvc.seed import load_teams
from meridian_jobsvc.service import ClusterPolicy, JobService
from meridian_jobsvc.types import Checkpoint, DrainedJob, JobSpec, JobState, JobStatus, Node
from meridian_jobsvc.worker import WorkerRuntime

__all__ = [
    "Autoscaler",
    "Checkpoint",
    "ClusterNode",
    "ClusterPolicy",
    "CredentialRefused",
    "CredentialUnavailable",
    "DependencyError",
    "DrainError",
    "DrainedJob",
    "Job",
    "JobIdentity",
    "JobLogs",
    "JobNotFound",
    "JobQueue",
    "JobRequest",
    "JobService",
    "JobSpec",
    "JobState",
    "JobStateConflict",
    "JobStatus",
    "Jobs",
    "Ledger",
    "Node",
    "NodeDaemon",
    "NodeLifecycle",
    "NodeNotFound",
    "NodeState",
    "NodeUnavailable",
    "Nodes",
    "NotJobOwner",
    "PriorityError",
    "ProcessOutput",
    "ProcessState",
    "ProcessStatus",
    "QueuedJob",
    "Quota",
    "QuotaExceeded",
    "ScalePolicy",
    "Schedule",
    "ScheduleNotFound",
    "ScheduleRequest",
    "Scheduler",
    "Schedules",
    "SchedulingError",
    "SpecSyntaxError",
    "Spend",
    "SubmissionRefused",
    "Usage",
    "WorkerRuntime",
    "WorkloadIssuer",
    "__version__",
    "load_teams",
    "parse_spec",
]
