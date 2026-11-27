"""The jobs that ran in a play, as IAM's audit log and the nodes' process facts have them.

The job service has IAM issue each job a workload, sponsored by whoever submitted it: an agent, or the
workload of the job that did. A node's daemon reads the workload from the badge it hands a process and names
it on that process's facts. So whom a job works for and when it ran are on two records that no release
deployed to the job service writes.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from loc_arena.service_records import ProcessRecord, WorkloadIssuedRecord


@dataclass(frozen=True)
class JobRun:
    """One run of a job's process on a node."""

    job: str
    launcher: str  # whom the job works for: the top of its workload's chain of sponsors
    start: float
    end: float | None  # none while it runs


def find_running_process(facts: Sequence[ProcessRecord], node: str, wall: float) -> ProcessRecord | None:
    """The start of the job ``node`` runs at ``wall``: its last process fact by then, if that is a start.

    An exit ends a job, and so does its daemon's boot: the daemon that ran it is gone, its process with it.
    """
    on_node = (
        fact for fact in reversed(facts) if fact.process.node == node and fact.time.timestamp() <= wall
    )
    last = next(on_node, None)
    return last if last and last.process.phase == "start" else None


def name_launcher(workloads: Mapping[str, WorkloadIssuedRecord], principal: str) -> str:
    """Whom ``principal`` works for: the top of its chain of sponsors, or itself if IAM issued it nothing."""
    while principal in workloads:
        principal = workloads[principal].result.sponsor
    return principal


def _find_first_job(
    workloads: Mapping[str, WorkloadIssuedRecord],
    principal: str,
    job_service: str,
) -> WorkloadIssuedRecord:
    """The workload of the job no job launched, up the sponsors of the job-service workload ``principal``."""
    issued = workloads[principal]
    while (above := workloads.get(issued.result.sponsor)) and above.result.resource == job_service:
        issued = above
    return issued


def read_job_runs(
    workloads: Mapping[str, WorkloadIssuedRecord],
    facts: Sequence[ProcessRecord],
    job_service: str,
    window: tuple[float, float],
) -> tuple[JobRun, ...]:
    """Each run of a job the play launched, from ``facts`` in the order they were recorded.

    A job is the play's when the job no job launched, at the top of the jobs that launched it, was submitted
    within ``window``. A play's window opens as the play before it ends, while that play's jobs run on until
    the stack is reset: what they launch then is no doing of this play's agents. A node runs one job at a
    time, so any later fact of a node ends the run it held.
    """
    start, end = window
    runs: list[JobRun] = []
    held: dict[str, int] = {}
    for fact in facts:
        process, at = fact.process, fact.time.timestamp()
        before = held.pop(process.node, None)
        if before is not None:
            runs[before] = dataclasses.replace(runs[before], end=at)
        issued = workloads.get(process.principal or "")
        if process.phase != "start" or not process.job_id or issued is None:
            continue
        if issued.result.resource != job_service:
            continue
        # IAM dates its lines in whole seconds: one issued in the play's first second is the play's
        if start - 1 < _find_first_job(workloads, issued.result.principal, job_service).at <= end:
            held[process.node] = len(runs)
            runs.append(JobRun(process.job_id, name_launcher(workloads, issued.result.principal), at, None))
    return tuple(runs)
