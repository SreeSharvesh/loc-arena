"""The job service: Meridian's cluster, which keeps track of every job and gets it run on a node of its fleet.

``JobService`` admits each job to the dependency-aware priority queue. Each ``reconcile`` pass starts the
ready ones, in the queue's order, on the first free node whose cpu and memory fit, while the job's team is
within the cluster's caps (``ClusterPolicy``); a node holds one job at a time. The service runs nothing
itself: a daemon on each node runs the job's command as a process (``meridian_jobsvc.fleet``), and each pass
reads the daemons, so a job's state follows its process, never a poll of the service. A job runs until its
process exits, then has ``succeeded`` on exit code 0 and ``failed`` otherwise; a cancelled job is
``cancelling`` until its process is gone, then ``cancelled``; a held job waits until it is released; a job
waiting on one that did not succeed fails. A drained node starts no new job, and once the cluster's grace is
over its job's process is stopped and the job queued again as its next attempt, to run elsewhere. Only a job's
owner cancels, holds or releases it, unless an operator, whom the central authority checks, does. Schedules
(``meridian_jobsvc.schedules``) submit their runs on the passes they are due. Each change of a job's state is
one JSON line of the service's record, holding the job as it is then.

Every job runs as an identity of its own, never as its submitter: before a job is queued, the central
authority issues it one from the credential its submitter presented (``meridian_jobsvc.authority``), and a
schedule's runs share the one it was issued when it was created. A job's process gets each of its secrets in
an environment variable (``authority.credential_variable``). The service keeps them in its database until the
job ends, so a drained job's next attempt runs as the same identity; no record or answer holds one. The
cluster's seed (``meridian_jobsvc.seed``), other teams' jobs and schedules, is the service's before any call:
each its owner's, holding no identity, since no owner of it presented a credential, and so marked seed.

The service keeps what it holds in one database (``meridian_jobsvc.state``), and each change it makes under
its lock is one transaction of it: kept whole, and then on the record, or not at all. A service whose process
starts again on the same database holds every job, schedule, identity, drain and output the last one held,
gives out no id again, and follows each running job on its node as before, since a job's process is its
node's daemon's. How a process exited is kept before its node is reset, and that a drain stops a job before
its process is stopped, so a service that ends in between loses neither. A job recorded as placed whose node
holds no such job, with no exit kept of it, has failed: the service ended before it reached the node.

Each attempt's CPU-hours, its cpus times its wall time from its start to the pass that saw it end, are charged
to the job's team in the ledger (``meridian_jobsvc.accountant``). A job's team is its submitter's in the
cluster's seed (``meridian_jobsvc.seed``), and a job a job submitted counts against its submitter's team in
turn.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, NonNegativeFloat, PositiveFloat, PositiveInt, SecretStr

from meridian_common.jobclient.models import JobSpec, JobState
from meridian_common.records import Database
from meridian_jobsvc.authority import JobIdentity, WorkloadIssuer
from meridian_jobsvc.errors import (
    DependencyError,
    JobNotFound,
    JobStateConflict,
    NodeNotFound,
    NotJobOwner,
    PriorityError,
    ScheduleNotFound,
    SchedulingError,
)
from meridian_jobsvc.fleet import NodeDaemon, NodeUnavailable, ProcessOutput, ProcessState
from meridian_jobsvc.models import ClusterNode, Job, JobLogs, JobRequest, NodeState, Usage
from meridian_jobsvc.schedules import Schedule, ScheduleRequest
from meridian_jobsvc.seed import ClusterSeed
from meridian_jobsvc.state import ClusterState, KeptExit
from meridian_jobsvc.types import Node

LAUNCH_JOB = "submit_job"  # the operations the authority issues identities through: a job's,
LAUNCH_SCHEDULE = "create_schedule"  # and a schedule's, which its runs share
SECONDS_PER_HOUR = 3600
_PLACED = (JobState.RUNNING, JobState.CANCELLING)
_UNFINISHED = (JobState.PENDING, JobState.HELD, *_PLACED)


class ClusterPolicy(BaseModel):
    """What the cluster allows: the highest priority a job asks, each team's caps, and a drained job's grace.

    A cap of ``None`` is none.
    """

    model_config = ConfigDict(frozen=True)

    max_priority: int | None = None
    team_running_jobs: PositiveInt | None = None  # the most jobs of one team running at once
    team_cpu_hours: PositiveFloat | None = None  # the CPU-hours past which a team starts no new job
    drain_grace_seconds: NonNegativeFloat = 0.0  # how long a drained node's job runs on before it is requeued

    def admits(self, team: str, running: Mapping[str, int], used: Mapping[str, float]) -> bool:
        """Whether a job of ``team`` may start, given each team's ``running`` jobs and ``used`` CPU-hours."""
        return (self.team_running_jobs is None or running.get(team, 0) < self.team_running_jobs) and (
            self.team_cpu_hours is None or used.get(team, 0.0) < self.team_cpu_hours
        )


class JobService:
    """Every job and schedule by id, the queue of the jobs waiting to start, and the fleet that runs them."""

    def __init__(  # noqa: PLR0913 - the fleet, the record, the authority, and the cluster's own rules
        self,
        daemons: Sequence[NodeDaemon],
        write_record: Callable[[str], None],
        issuer: WorkloadIssuer,
        policy: ClusterPolicy | None = None,
        teams: Mapping[str, str] | None = None,
        *,
        admit_submitter: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        database: Database | None = None,
    ) -> None:
        """Run jobs on the nodes of ``daemons``, first free one first, each as an identity from ``issuer``.

        ``policy`` is what the cluster allows, and ``teams`` each principal's team; a principal on no team is
        a team of its own. ``admit_submitter`` is the deployment's own say on who submits, beside the
        authority's: it raises ``SubmissionRefused`` for one it refuses, and is asked of the principal a job
        works for, never of a job. Each change of a job's state is written with ``write_record``, at
        ``clock``'s time. The service holds what ``database`` holds, and keeps what it holds there; without
        one it keeps it in memory.
        """
        self._daemons = {daemon.node.node_id: daemon for daemon in daemons}
        self._issuer = issuer
        self._policy = policy or ClusterPolicy()
        self._teams = dict(teams or {})
        self._admit_submitter = admit_submitter or _admit_anyone
        self._clock = clock or _utc_now
        # What a restart keeps, with its queue, its ledger and its lock, which is never held while a node's
        # daemon or the authority is called.
        self._state = state = ClusterState(database, write_record)
        self._jobs, self._schedules, self._sponsors = state.jobs, state.schedules, state.sponsors
        self._identities, self._schedule_identities = state.identities, state.schedule_identities
        self._placed, self._drains, self._evicted = state.placed, state.drains, state.evicted
        self._exits, self._outputs = state.exits, state.outputs
        self._changed = threading.Event()  # a job was submitted, released or cancelled: a pass comes at once
        self._stopped = threading.Event()  # ``stop`` was called: ``run`` ends after the pass under way

    def submit(self, request: JobRequest, owner: str, credential: str | None) -> Job:
        """Queue ``request`` as ``owner``'s job: it starts once its dependencies succeed and a node is free.

        It runs as the identity the authority issues it, under its id and its name, from ``credential``: the
        one ``owner`` presented. Raises ``SchedulingError`` for a job larger than every node,
        ``PriorityError`` for one above the cluster's highest priority, ``SubmissionRefused`` when the
        deployment refuses its submitter, ``DependencyError`` for a dependency that is no job, and
        ``CredentialRefused`` or ``CredentialUnavailable`` when it could not be issued its identity; a refused
        job is not queued.
        """
        with self._state.locked():
            self._admit(request, owner)
            job_id = self._state.reserve()  # no job yet: no pass places it until it is issued
        try:
            identity = self._issuer.issue(credential, LAUNCH_JOB, job_id, request.name)
        except Exception:
            with self._state.locked():
                self._state.refuse(job_id)
            raise
        with self._state.locked():
            job = self._add(job_id, request, owner, identity)
        self._changed.set()
        return job

    def seed(self, seed: ClusterSeed) -> None:
        """Queue ``seed``'s jobs, held where it says, and keep its schedules: each its owner's, no identity.

        Call it before the service takes a call, so the seeded jobs are ``job-1`` onwards in the seed's order.
        A service that holds a job or a schedule keeps what it holds: it started again on its database.
        """
        with self._state.locked():
            if self._jobs or self._schedules:
                return
            for seeded in seed.jobs:
                self._require_dependencies(seeded.job)
                job = self._add(self._state.reserve(), seeded.job, seeded.owner, None)
                if seeded.held:
                    self._state.queue.dispatch(job.id)
                    self._record(job, state=JobState.HELD)
            for seeded in seed.schedules:
                self._keep_schedule(self._state.reserve_schedule(), seeded.schedule, seeded.owner, None)
        self._changed.set()

    def list_jobs(self) -> list[Job]:
        """Every job, oldest first."""
        with self._state.locked():
            now = self._clock()
            return [_reported(job, now) for job in self._jobs.values()]

    def get_job(self, job_id: str) -> Job:
        """The job ``job_id``; raises ``JobNotFound`` if there is none."""
        with self._state.locked():
            return _reported(self._find(job_id), self._clock())

    def logs(self, job_id: str) -> JobLogs:
        """The tails of the job's output: read from its node while it runs, else as it was when it ended.

        Raises ``NodeUnavailable`` when the node of a running job does not answer.
        """
        with self._state.locked():
            job = self._find(job_id)
            output = self._outputs.get(job_id)
        if output is None and job.node is not None and job.state in _PLACED:
            live = self._daemons[job.node].output()
            with self._state.locked():  # the job may have ended meanwhile, and its node taken the next one
                output = self._outputs.get(job_id, live if live.job_id == job_id else None)
        output = output or ProcessOutput(job_id=job_id, stdout="", stderr="")
        return JobLogs(job_id=job_id, stdout=output.stdout, stderr=output.stderr)

    def cancel(self, job_id: str, owned_by: str | None) -> Job:
        """Cancel a job: one waiting to start at once, a running one once its process is gone.

        ``owned_by`` is the caller whose job it must be, or ``None`` for an operator, whom the authority has
        checked. Raises ``NotJobOwner`` for another's job and ``JobStateConflict`` for one that has ended;
        cancelling a job again while its process is ending changes nothing.
        """
        with self._state.locked():
            job = self._owned(job_id, owned_by)
            if job.state in (JobState.PENDING, JobState.HELD):
                if job.state is JobState.PENDING:
                    self._state.queue.dispatch(job_id)
                self._state.queue.notify_failed(job_id)
                self._identities.pop(job_id, None)
                job = self._record(job, state=JobState.CANCELLED, ended_at=self._clock())
            elif job.state is JobState.RUNNING:
                job = self._record(job, state=JobState.CANCELLING)
            elif job.state is not JobState.CANCELLING:
                raise JobStateConflict(f"job {job_id!r} has ended: it is {job.state.value}", path=job_id)
            job = _reported(job, self._clock())
        self._changed.set()
        return job

    def hold(self, job_id: str, owned_by: str | None) -> Job:
        """Keep a waiting job from starting until it is released; holding a held job changes nothing.

        ``owned_by`` as for ``cancel``. Raises ``JobStateConflict`` for a job that has started or ended.
        """
        with self._state.locked():
            job = self._owned(job_id, owned_by)
            if job.state is JobState.PENDING:
                self._state.queue.dispatch(job_id)
                job = self._record(job, state=JobState.HELD)
            elif job.state is not JobState.HELD:
                raise JobStateConflict(
                    f"job {job_id!r} is {job.state.value}: only a waiting job is held",
                    path=job_id,
                )
            return job

    def release(self, job_id: str, owned_by: str | None) -> Job:
        """Let a held job start, in the place it had in the queue; releasing a waiting job changes nothing.

        ``owned_by`` as for ``cancel``. Raises ``JobStateConflict`` for a job that has started or ended.
        """
        with self._state.locked():
            job = self._owned(job_id, owned_by)
            if job.state is JobState.HELD:
                self._state.queue.requeue(job_id)
                job = self._record(job, state=JobState.PENDING)
            elif job.state is not JobState.PENDING:
                raise JobStateConflict(
                    f"job {job_id!r} is {job.state.value}: only a held job is released",
                    path=job_id,
                )
        self._changed.set()
        return job

    def create_schedule(self, request: ScheduleRequest, owner: str, credential: str | None) -> Schedule:
        """Have ``request``'s job submitted as ``owner``'s each time a run is due, every run as one identity.

        The authority issues the schedule its identity now, from ``credential``, with the schedule's id as
        reference. Raises what ``submit`` raises for the job; a refused schedule is not kept.
        """
        with self._state.locked():
            self._admit(request.job, owner)
            schedule_id = self._state.reserve_schedule()  # never given again, as the authority keys on it
        identity = self._issuer.issue(credential, LAUNCH_SCHEDULE, schedule_id, request.job.name)
        with self._state.locked():
            return self._keep_schedule(schedule_id, request, owner, identity)

    def list_schedules(self) -> list[Schedule]:
        """Every schedule, oldest first, deleted ones included."""
        with self._state.locked():
            return list(self._schedules.values())

    def get_schedule(self, schedule_id: str) -> Schedule:
        """The schedule ``schedule_id``, with its runs; raises ``ScheduleNotFound`` if there is none."""
        with self._state.locked():
            return self._find_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str, owned_by: str | None) -> Schedule:
        """Stop a schedule's future runs; the runs it started carry on, and deleting it again changes nothing.

        ``owned_by`` as for ``cancel``: raises ``NotJobOwner`` for another's schedule.
        """
        with self._state.locked():
            schedule = self._find_schedule(schedule_id)
            if owned_by is not None and schedule.owner != owned_by:
                raise NotJobOwner(
                    f"schedule {schedule_id!r} is {schedule.owner}'s, not yours",
                    path=schedule_id,
                )
            if schedule.deleted_at is None:
                self._schedule_identities.pop(schedule_id, None)
                schedule = schedule.model_copy(update={"deleted_at": self._clock(), "next_run_at": None})
                self._schedules[schedule_id] = schedule
            return schedule

    def list_nodes(self) -> list[ClusterNode]:
        """Every node of the fleet: what a job on it gets, whether it takes one, and its job."""
        with self._state.locked():
            return [self._describe(node_id) for node_id in self._daemons]

    def drain(self, node_id: str) -> ClusterNode:
        """Start no new job on ``node_id``; once the grace is over, requeue its job as its next attempt.

        The job keeps its id and its identity, and runs again as a fresh process on another node; one whose
        process ends within the grace ends as it would have. Draining a drained node changes nothing. Raises
        ``NodeNotFound`` for a node the cluster does not have.
        """
        with self._state.locked():
            if node_id not in self._daemons:
                raise NodeNotFound(f"no node {node_id!r}", path=node_id)
            grace = timedelta(seconds=self._policy.drain_grace_seconds)
            self._drains.setdefault(node_id, self._clock() + grace)
            node = self._describe(node_id)
        self._changed.set()
        return node

    def usage(self) -> Usage:
        """The CPU-hours each team's jobs and each job have used, a running job's to now."""
        with self._state.locked():
            now = self._clock()
            jobs = {job.id: _reported(job, now).cpu_hours for job in self._jobs.values()}
            return Usage(teams=self._team_usage(now), jobs=jobs)

    def reconcile(self) -> None:
        """One pass over the fleet: follow each node's job, submit the schedules' due runs, start ready jobs.

        Following a job ends or requeues it once its process is gone, and stops a cancelled job's process, and
        a drained node's job's once its grace is over. A node whose daemon does not answer is passed over
        until the next pass.
        """
        for node_id, daemon in self._daemons.items():
            with self._state.locked():
                job = self._jobs.get(self._placed.get(node_id, ""))
            if job is not None:
                with contextlib.suppress(NodeUnavailable):
                    self._follow(daemon, job)
        with self._state.locked():
            self._submit_due_runs(self._clock())
        for daemon, job, environment in self._place_ready_jobs():
            try:
                daemon.start(job.id, job.command, job.owner, environment, job.attempt)
            except NodeUnavailable as error:
                with contextlib.suppress(NodeUnavailable):
                    daemon.reset()  # in case it started the job but the answer was lost
                with self._state.locked():
                    self._end(job.id, None, f"its node did not start it: {error}", self._clock())

    def run(self, poll_seconds: float) -> None:
        """Reconcile until ``stop``: every ``poll_seconds``, and at once after a submit, release or cancel."""
        while True:
            self._changed.clear()
            if self._stopped.is_set():  # looked at once the event is clear, so no ``stop`` goes unseen
                return
            try:
                self.reconcile()
            except Exception:  # one pass that fails must not stop the cluster: the next one tries again
                logging.getLogger(__name__).exception("a pass over the fleet failed")
            self._changed.wait(poll_seconds)

    def stop(self) -> None:
        """End ``run`` once the pass under way is over, so no job is left half placed."""
        self._stopped.set()
        self._changed.set()

    def _follow(self, daemon: NodeDaemon, job: Job) -> None:
        """Stop a cancelled job's process, or a drained node's job's once its grace is over.

        Once the process has ended, keep how it exited and what it wrote, then reset its node, then requeue
        the job the drain stopped or end it as its process ended. A job whose node holds no such job ends
        as the exit kept of it says, and has failed when none was: its process is gone, and nobody saw how.
        """
        status = daemon.status()
        if status.job_id == job.id and status.state is ProcessState.RUNNING:
            if job.state is JobState.CANCELLING or self._evicts(job):
                daemon.stop()
            return
        if status.job_id == job.id:
            output = daemon.output()
            with (
                self._state.locked()
            ):  # before the node forgets it, so no restart loses how and when it ended
                self._exits.setdefault(job.id, KeptExit(exit_code=status.exit_code, seen_at=self._clock()))
                self._outputs[job.id] = output
        daemon.reset()
        with self._state.locked():
            job = self._jobs[job.id]  # as it is now: it may have been cancelled meanwhile
            kept = self._exits.pop(job.id, None)
            now = self._clock() if kept is None else kept.seen_at
            stopped = job.id in self._evicted and job.state is JobState.RUNNING
            if stopped and (kept is None or kept.exit_code != 0):  # one that exited 0 by itself has succeeded
                self._outputs.pop(job.id, None)
                self._requeue(job, now)
            elif kept is None:
                self._outputs[job.id] = ProcessOutput(job_id=job.id, stdout="", stderr="")
                self._end(job.id, None, "its node lost it", now)
            else:
                self._end(job.id, kept.exit_code, "", now)

    def _evicts(self, job: Job) -> bool:
        """Whether the grace of ``job``'s drained node is over: if so, mark the job as one the drain stops.

        It is marked before its process is stopped, so a process the stop ended is queued again, never
        failed as killed, whatever happens to the service in between.
        """
        with self._state.locked():
            ends = self._drains.get(job.node or "")
            if ends is None or self._clock() < ends:
                return False
            self._evicted[job.id] = True
            return True

    def _submit_due_runs(self, now: datetime) -> None:
        """Queue a run of each schedule that is due and whose last run has ended; lock held.

        A due time that finds the last run unfinished is skipped.
        """
        for schedule in list(self._schedules.values()):
            if schedule.next_run_at is None or schedule.next_run_at > now:
                continue
            runs = schedule.runs
            if not runs or self._jobs[runs[-1]].state not in _UNFINISHED:
                job_id = self._state.reserve()
                self._add(
                    job_id,
                    schedule.job,
                    schedule.owner,
                    self._schedule_identities.get(schedule.id),  # none for a seeded schedule
                    schedule.id,
                )
                runs = [*runs, job_id]
            due = schedule.next_run(schedule.created_at, now)
            self._schedules[schedule.id] = schedule.model_copy(update={"runs": runs, "next_run_at": due})

    def _place_ready_jobs(self) -> list[tuple[NodeDaemon, Job, dict[str, SecretStr]]]:
        """Fail the jobs waiting on one that failed; place each ready job on the first free node it fits.

        A job starts only while its team is within the cluster's caps, and never on a drained node. Each
        placed job comes with what its process gets: its own variables and its identity's secrets.
        """
        with self._state.locked():
            now = self._clock()
            for blocked in self._state.queue.blocked():
                self._state.queue.dispatch(blocked.job_id)
                self._state.queue.notify_failed(blocked.job_id)
                self._identities.pop(blocked.job_id, None)
                self._record(
                    self._jobs[blocked.job_id],
                    state=JobState.FAILED,
                    ended_at=now,
                    message="a job it depends on did not succeed",
                )
            free = [
                node_id
                for node_id in self._daemons
                if node_id not in self._placed and node_id not in self._drains
            ]
            running = Counter(self._jobs[job_id].team for job_id in self._placed.values())
            used = self._team_usage(now)
            placed = []
            for ready in self._state.queue.ready():
                job = self._jobs[ready.job_id]
                if not self._policy.admits(job.team, running, used):
                    continue
                node_id = next((node for node in free if _fits(ready.spec, self._daemons[node].node)), None)
                if node_id is None:
                    continue
                free.remove(node_id)
                running[job.team] += 1
                self._state.queue.dispatch(ready.job_id)
                self._placed[node_id] = ready.job_id
                job = self._record(job, state=JobState.RUNNING, node=node_id, started_at=now, message="")
                placed.append((self._daemons[node_id], job, self._process_environment(job)))
        return placed

    def _requeue(self, job: Job, now: datetime) -> None:
        """Queue a job whose drained node stopped it at ``now`` again, as its next attempt; lock held."""
        self._evicted.pop(job.id, None)
        self._placed.pop(job.node or "", None)
        cpu_hours = self._charge(job, now)
        self._state.queue.requeue(job.id)
        self._record(
            job,
            state=JobState.PENDING,
            node=None,
            started_at=None,
            attempt=job.attempt + 1,
            cpu_hours=cpu_hours,
            message="its node was drained: it runs again elsewhere",
        )

    def _end(self, job_id: str, exit_code: int | None, message: str, now: datetime) -> None:
        """Record that a placed job ended at ``now``, release the jobs waiting on it, and free its node.

        It has succeeded if its process exited 0, and failed otherwise; a job being cancelled ends
        ``cancelled``, however its process ended. Lock held.
        """
        job = self._jobs[job_id]
        state = JobState.SUCCEEDED if exit_code == 0 else JobState.FAILED
        state = JobState.CANCELLED if job.state is JobState.CANCELLING else state
        if state is JobState.SUCCEEDED:
            self._state.queue.notify_succeeded(job_id)
        else:
            self._state.queue.notify_failed(job_id)
        if job.node is not None:
            self._placed.pop(job.node, None)
        self._identities.pop(job_id, None)
        self._evicted.pop(job_id, None)
        hours = self._charge(job, now)
        self._record(job, state=state, ended_at=now, exit_code=exit_code, cpu_hours=hours, message=message)

    def _admit(self, request: JobRequest, submitter: str) -> None:
        """Raise unless the cluster and the deployment admit ``request`` from ``submitter``; lock held."""
        if not any(_fits(request, daemon.node) for daemon in self._daemons.values()):
            raise SchedulingError(
                f"job {request.name!r} asks for {request.cpu:g} cpus and {request.memory_mb} MB, more than "
                "any node of the cluster has",
                path=request.name,
            )
        highest = self._policy.max_priority
        if highest is not None and request.priority > highest:
            raise PriorityError(
                f"job {request.name!r} asks for priority {request.priority}, above the cluster's {highest}",
                path=request.name,
            )
        self._admit_submitter(self._sponsor_of(submitter))
        self._require_dependencies(request)

    def _require_dependencies(self, request: JobRequest) -> None:
        """Raise ``DependencyError`` unless every job ``request`` waits on is a known id; lock held."""
        for dependency in request.depends_on:
            if not self._state.knows(dependency):
                raise DependencyError(
                    f"unknown dependency {dependency!r} for job {request.name!r}",
                    path=dependency,
                )

    def _add(
        self,
        job_id: str,
        request: JobRequest,
        owner: str,
        identity: JobIdentity | None,
        schedule_id: str | None = None,
    ) -> Job:
        """Queue ``request`` under the reserved ``job_id`` as ``owner``'s, running as ``identity``; lock held.

        A job with no identity is seeded.
        """
        self._state.admit(job_id, request)
        if identity is not None:
            self._identities[job_id] = identity
            self._sponsors.setdefault(identity.principal, owner)
        sponsor = self._sponsor_of(owner)
        job = Job(
            id=job_id,
            **request.model_dump(),
            owner=owner,
            principal=None if identity is None else identity.principal,
            team=self._teams.get(sponsor, sponsor),
            schedule_id=schedule_id,
            seed=identity is None,
            state=JobState.PENDING,
            submitted_at=self._clock(),
        )
        return self._record(job)

    def _keep_schedule(
        self,
        schedule_id: str,
        request: ScheduleRequest,
        owner: str,
        identity: JobIdentity | None,
    ) -> Schedule:
        """Keep ``owner``'s schedule, its runs to run as ``identity``: a seeded one has none. Lock held."""
        if identity is not None:
            self._sponsors[identity.principal] = owner
            self._schedule_identities[schedule_id] = identity
        now = self._clock()
        self._schedules[schedule_id] = Schedule(
            **request.model_dump(),
            id=schedule_id,
            owner=owner,
            principal=None if identity is None else identity.principal,
            seed=identity is None,
            created_at=now,
            next_run_at=request.next_run(now, now),
        )
        return self._schedules[schedule_id]

    def _sponsor_of(self, principal: str) -> str:
        """The principal at the top of ``principal``'s chain of jobs and schedules: whom its work is for.

        A chain that comes back on itself, which only an authority reusing a principal could make, ends there.
        """
        seen = {principal}
        while (sponsor := self._sponsors.get(principal)) is not None and sponsor not in seen:
            seen.add(sponsor)
            principal = sponsor
        return principal

    def _charge(self, job: Job, now: datetime) -> float:
        """Charge a placed job's current attempt to its team; returns its CPU-hours with it. Lock held."""
        hours = _running_hours(job, now)
        self._state.charge(job.team, hours)
        return job.cpu_hours + hours

    def _team_usage(self, now: datetime) -> dict[str, float]:
        """Each team's CPU-hours: its jobs' ended attempts, and their running ones to ``now``. Lock held."""
        used = {team: spend.compute for team, spend in self._state.ledger.report().items()}
        for job_id in self._placed.values():
            job = self._jobs[job_id]
            used[job.team] = used.get(job.team, 0.0) + _running_hours(job, now)
        return used

    def _process_environment(self, job: Job) -> dict[str, SecretStr]:
        """What ``job``'s process gets: its own variables, and its identity's secrets, if any. Lock held."""
        own = {name: SecretStr(value) for name, value in job.environment.items()}
        identity = self._identities.get(job.id)
        return own if identity is None else own | identity.environment()

    def _describe(self, node_id: str) -> ClusterNode:
        node = self._daemons[node_id].node
        job_id = self._placed.get(node_id)
        if node_id in self._drains:
            state = NodeState.DRAINING if job_id else NodeState.DRAINED
        else:
            state = NodeState.BUSY if job_id else NodeState.FREE
        return ClusterNode(
            id=node_id,
            cpu=node.cpu_capacity,
            memory_mb=node.memory_mb,
            state=state,
            job_id=job_id,
        )

    def _owned(self, job_id: str, owned_by: str | None) -> Job:
        job = self._find(job_id)
        if owned_by is not None and job.owner != owned_by:
            raise NotJobOwner(f"job {job_id!r} is {job.owner}'s, not yours", path=job_id)
        return job

    def _find(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise JobNotFound(f"no job {job_id!r}", path=job_id)
        return self._jobs[job_id]

    def _find_schedule(self, schedule_id: str) -> Schedule:
        if schedule_id not in self._schedules:
            raise ScheduleNotFound(f"no schedule {schedule_id!r}", path=schedule_id)
        return self._schedules[schedule_id]

    def _record(self, job: Job, **changes: object) -> Job:
        """Keep ``job`` with ``changes``, on the record too once the section is kept; lock held."""
        job = job.model_copy(update=changes)
        self._jobs[job.id] = job
        line = {"wall_ts": self._clock().isoformat(), "job": job.model_dump(mode="json")}
        self._state.record(json.dumps(line))
        return job


def _fits(job: JobSpec | JobRequest, node: Node) -> bool:
    """Whether ``node`` has the cpus and memory ``job`` asks for."""
    return job.cpu <= node.cpu_capacity and job.memory_mb <= node.memory_mb


def _running_hours(job: Job, now: datetime) -> float:
    """The CPU-hours a placed job's current attempt has used by ``now``; none for any other job."""
    if job.state not in _PLACED or job.started_at is None:
        return 0.0
    return job.cpu * max((now - job.started_at).total_seconds(), 0.0) / SECONDS_PER_HOUR


def _reported(job: Job, now: datetime) -> Job:
    """``job`` as the service answers it: with its running attempt's CPU-hours to ``now``."""
    return job.model_copy(update={"cpu_hours": job.cpu_hours + _running_hours(job, now)})


def _admit_anyone(submitter: str) -> None:
    _ = submitter


def _utc_now() -> datetime:
    return datetime.now(UTC)
