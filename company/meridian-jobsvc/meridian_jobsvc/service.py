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
an environment variable (``credential_variable``). The service keeps them until the job ends, so a drained
job's next attempt runs as the same identity; no record or answer holds one. The cluster's seed
(``meridian_jobsvc.seed``), other teams' jobs and schedules, is the service's before any call: each its
owner's, holding no identity, since no owner of it presented a credential, and so marked seed.

Each attempt's CPU-hours, its cpus times its wall time from its start to the pass that saw it end, are charged
to the job's team in the ledger (``meridian_jobsvc.accountant``). A job's team is its submitter's in the
cluster's seed (``meridian_jobsvc.seed``), and a job a job submitted counts against its submitter's team in
turn.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, NonNegativeFloat, PositiveFloat, PositiveInt, SecretStr

from meridian_common.jobclient.models import JobSpec, JobState
from meridian_jobsvc.accountant.ledger import Ledger
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
from meridian_jobsvc.queue.jobqueue import JobQueue
from meridian_jobsvc.schedules import Schedule, ScheduleRequest
from meridian_jobsvc.seed import ClusterSeed
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
    ) -> None:
        """Run jobs on the nodes of ``daemons``, first free one first, each as an identity from ``issuer``.

        ``policy`` is what the cluster allows, and ``teams`` each principal's team; a principal on no team is
        a team of its own. ``admit_submitter`` is the deployment's own say on who submits, beside the
        authority's: it raises ``SubmissionRefused`` for one it refuses, and is asked of the principal a job
        works for, never of a job. Each change of a job's state is written with ``write_record``, at
        ``clock``'s time.
        """
        self._daemons = {daemon.node.node_id: daemon for daemon in daemons}
        self._write_record = write_record
        self._issuer = issuer
        self._policy = policy or ClusterPolicy()
        self._teams = dict(teams or {})
        self._admit_submitter = admit_submitter or _admit_anyone
        self._clock = clock or _utc_now
        self._queue = JobQueue()
        self._ledger = Ledger()  # each team's CPU-hours, of its jobs' ended attempts
        self._jobs: dict[str, Job] = {}
        self._identities: dict[str, JobIdentity] = {}  # each job's, until it ends
        self._sponsors: dict[str, str] = {}  # each job's and schedule's own identity, and who launched it
        self._schedules: dict[str, Schedule] = {}
        self._schedule_numbers = itertools.count(1)  # never repeats an id, as the authority keys on it
        self._schedule_identities: dict[str, JobIdentity] = {}  # each schedule's, until it is deleted
        self._placed: dict[str, str] = {}  # the id of the job on each node that holds one
        self._drains: dict[str, datetime] = {}  # each drained node, and when its job's grace is over
        self._evicted: set[str] = set()  # the jobs whose process the service stopped for a drain
        self._outputs: dict[str, ProcessOutput] = {}  # each ended job's output, kept once its node is reset
        self._lock = threading.Lock()  # never held while a node's daemon or the authority is called
        self._changed = threading.Event()  # a job was submitted, released or cancelled: a pass comes at once

    def submit(self, request: JobRequest, owner: str, credential: str | None) -> Job:
        """Queue ``request`` as ``owner``'s job: it starts once its dependencies succeed and a node is free.

        It runs as the identity the authority issues it, under its id and its name, from ``credential``: the
        one ``owner`` presented. Raises ``SchedulingError`` for a job larger than every node,
        ``PriorityError`` for one above the cluster's highest priority, ``SubmissionRefused`` when the
        deployment refuses its submitter, ``DependencyError`` for a dependency that is no job, and
        ``CredentialRefused`` or ``CredentialUnavailable`` when it could not be issued its identity; a refused
        job is not queued.
        """
        with self._lock:
            job_id = self._queue.submit(self._admit(request, owner))  # no pass places it until it is issued
        try:
            identity = self._issuer.issue(credential, LAUNCH_JOB, job_id, request.name)
        except Exception:
            with self._lock:
                self._queue.dispatch(job_id)
                self._queue.notify_failed(job_id)
            raise
        with self._lock:
            job = self._add(job_id, request, owner, identity)
        self._changed.set()
        return job

    def seed(self, seed: ClusterSeed) -> None:
        """Queue ``seed``'s jobs, held where it says, and keep its schedules: each its owner's, no identity.

        Call it before the service takes a call, so the seeded jobs are ``job-1`` onwards in the seed's order.
        """
        with self._lock:
            for seeded in seed.jobs:
                job = self._add(self._queue.submit(_spec(seeded.job)), seeded.job, seeded.owner, None)
                if seeded.held:
                    self._queue.dispatch(job.id)
                    self._record(job, state=JobState.HELD)
            for seeded in seed.schedules:
                schedule_id = f"schedule-{next(self._schedule_numbers)}"
                self._keep_schedule(schedule_id, seeded.schedule, seeded.owner, None)
        self._changed.set()

    def list_jobs(self) -> list[Job]:
        """Every job, oldest first."""
        with self._lock:
            now = self._clock()
            return [_reported(job, now) for job in self._jobs.values()]

    def get_job(self, job_id: str) -> Job:
        """The job ``job_id``; raises ``JobNotFound`` if there is none."""
        with self._lock:
            return _reported(self._find(job_id), self._clock())

    def logs(self, job_id: str) -> JobLogs:
        """The tails of the job's output: read from its node while it runs, else as it was when it ended.

        Raises ``NodeUnavailable`` when the node of a running job does not answer.
        """
        with self._lock:
            job = self._find(job_id)
            output = self._outputs.get(job_id)
        if output is None and job.node is not None and job.state in _PLACED:
            live = self._daemons[job.node].output()
            with self._lock:  # the job may have ended meanwhile, and its node taken the next one
                output = self._outputs.get(job_id, live if live.job_id == job_id else None)
        output = output or ProcessOutput(job_id=job_id, stdout="", stderr="")
        return JobLogs(job_id=job_id, stdout=output.stdout, stderr=output.stderr)

    def cancel(self, job_id: str, owned_by: str | None) -> Job:
        """Cancel a job: one waiting to start at once, a running one once its process is gone.

        ``owned_by`` is the caller whose job it must be, or ``None`` for an operator, whom the authority has
        checked. Raises ``NotJobOwner`` for another's job and ``JobStateConflict`` for one that has ended;
        cancelling a job again while its process is ending changes nothing.
        """
        with self._lock:
            job = self._owned(job_id, owned_by)
            if job.state in (JobState.PENDING, JobState.HELD):
                if job.state is JobState.PENDING:
                    self._queue.dispatch(job_id)
                self._queue.notify_failed(job_id)
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
        with self._lock:
            job = self._owned(job_id, owned_by)
            if job.state is JobState.PENDING:
                self._queue.dispatch(job_id)
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
        with self._lock:
            job = self._owned(job_id, owned_by)
            if job.state is JobState.HELD:
                self._queue.requeue(job_id)
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
        with self._lock:
            self._admit(request.job, owner)
            for dependency in request.job.depends_on:
                if dependency not in self._queue:
                    raise DependencyError(
                        f"unknown dependency {dependency!r} for job {request.job.name!r}",
                        path=dependency,
                    )
            schedule_id = f"schedule-{next(self._schedule_numbers)}"
        identity = self._issuer.issue(credential, LAUNCH_SCHEDULE, schedule_id, request.job.name)
        with self._lock:
            return self._keep_schedule(schedule_id, request, owner, identity)

    def list_schedules(self) -> list[Schedule]:
        """Every schedule, oldest first, deleted ones included."""
        with self._lock:
            return list(self._schedules.values())

    def get_schedule(self, schedule_id: str) -> Schedule:
        """The schedule ``schedule_id``, with its runs; raises ``ScheduleNotFound`` if there is none."""
        with self._lock:
            return self._find_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str, owned_by: str | None) -> Schedule:
        """Stop a schedule's future runs; the runs it started carry on, and deleting it again changes nothing.

        ``owned_by`` as for ``cancel``: raises ``NotJobOwner`` for another's schedule.
        """
        with self._lock:
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
        with self._lock:
            return [self._describe(node_id) for node_id in self._daemons]

    def drain(self, node_id: str) -> ClusterNode:
        """Start no new job on ``node_id``; once the grace is over, requeue its job as its next attempt.

        The job keeps its id and its identity, and runs again as a fresh process on another node; one whose
        process ends within the grace ends as it would have. Draining a drained node changes nothing. Raises
        ``NodeNotFound`` for a node the cluster does not have.
        """
        with self._lock:
            if node_id not in self._daemons:
                raise NodeNotFound(f"no node {node_id!r}", path=node_id)
            grace = timedelta(seconds=self._policy.drain_grace_seconds)
            self._drains.setdefault(node_id, self._clock() + grace)
            node = self._describe(node_id)
        self._changed.set()
        return node

    def usage(self) -> Usage:
        """The CPU-hours each team's jobs and each job have used, a running job's to now."""
        with self._lock:
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
            with self._lock:
                job = self._jobs.get(self._placed.get(node_id, ""))
            if job is not None:
                with contextlib.suppress(NodeUnavailable):
                    self._follow(daemon, job)
        with self._lock:
            self._submit_due_runs(self._clock())
        for daemon, job, environment in self._place_ready_jobs():
            try:
                daemon.start(job.id, job.command, job.owner, environment, job.attempt)
            except NodeUnavailable as error:
                with contextlib.suppress(NodeUnavailable):
                    daemon.reset()  # in case it started the job but the answer was lost
                with self._lock:
                    self._end(job.id, JobState.FAILED, None, f"its node did not start it: {error}")

    def run(self, poll_seconds: float) -> None:
        """Reconcile forever: every ``poll_seconds``, and at once after a job is submitted or cancelled."""
        while True:
            self._changed.clear()
            try:
                self.reconcile()
            except Exception:  # one pass that fails must not stop the cluster: the next one tries again
                logging.getLogger(__name__).exception("a pass over the fleet failed")
            self._changed.wait(poll_seconds)

    def _follow(self, daemon: NodeDaemon, job: Job) -> None:
        """Stop a cancelled job's process, or a drained node's job's once its grace is over.

        Once the process is gone, requeue the job the drain stopped, or keep its output and end it.
        """
        status = daemon.status()
        if status.job_id == job.id and status.state is ProcessState.RUNNING:
            if job.state is JobState.CANCELLING:
                daemon.stop()
            elif self._grace_is_over(job):
                daemon.stop()
                with self._lock:
                    self._evicted.add(job.id)
            return
        lost = status.job_id != job.id  # its daemon holds no such job: it restarted, and the process is gone
        output = ProcessOutput(job_id=job.id, stdout="", stderr="") if lost else daemon.output()
        daemon.reset()
        with self._lock:
            job = self._jobs[job.id]  # as it is now: it may have been cancelled meanwhile
            if job.id in self._evicted and job.state is JobState.RUNNING:
                self._requeue(job)
                return
            self._outputs[job.id] = output
            if lost:
                self._end(job.id, JobState.FAILED, None, "its node lost it")
            else:
                state = JobState.SUCCEEDED if status.exit_code == 0 else JobState.FAILED
                self._end(job.id, state, status.exit_code, "")

    def _grace_is_over(self, job: Job) -> bool:
        with self._lock:
            ends = self._drains.get(job.node or "")
            return ends is not None and self._clock() >= ends

    def _submit_due_runs(self, now: datetime) -> None:
        """Queue a run of each schedule that is due and whose last run has ended; lock held.

        A due time that finds the last run unfinished is skipped.
        """
        for schedule in list(self._schedules.values()):
            if schedule.next_run_at is None or schedule.next_run_at > now:
                continue
            runs = schedule.runs
            if not runs or self._jobs[runs[-1]].state not in _UNFINISHED:
                job_id = self._queue.submit(_spec(schedule.job))
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
        placed job comes with what its process gets: its own variables and its identity's secrets. A job still
        being issued its identity is not in the registry yet, and is left to a later pass.
        """
        with self._lock:
            now = self._clock()
            for blocked in self._queue.blocked():
                if blocked.job_id not in self._jobs:
                    continue
                self._queue.dispatch(blocked.job_id)
                self._queue.notify_failed(blocked.job_id)
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
            for ready in self._queue.ready():
                job = self._jobs.get(ready.job_id)
                if job is None or not self._policy.admits(job.team, running, used):
                    continue
                node_id = next((node for node in free if _fits(ready.spec, self._daemons[node].node)), None)
                if node_id is None:
                    continue
                free.remove(node_id)
                running[job.team] += 1
                self._queue.dispatch(ready.job_id)
                self._placed[node_id] = ready.job_id
                job = self._record(job, state=JobState.RUNNING, node=node_id, started_at=now, message="")
                placed.append((self._daemons[node_id], job, self._process_environment(job)))
        return placed

    def _requeue(self, job: Job) -> None:
        """Queue a job whose drained node stopped it again, as its next attempt; lock held."""
        self._evicted.discard(job.id)
        self._placed.pop(job.node or "", None)
        cpu_hours = self._charge(job, self._clock())
        self._queue.requeue(job.id)
        self._record(
            job,
            state=JobState.PENDING,
            node=None,
            started_at=None,
            attempt=job.attempt + 1,
            cpu_hours=cpu_hours,
            message="its node was drained: it runs again elsewhere",
        )

    def _end(self, job_id: str, state: JobState, exit_code: int | None, message: str) -> None:
        """Record that a placed job ended in ``state``, release the jobs waiting on it, and free its node.

        A job being cancelled ends ``cancelled``, however its process ended. Lock held.
        """
        job = self._jobs[job_id]
        state = JobState.CANCELLED if job.state is JobState.CANCELLING else state
        if state is JobState.SUCCEEDED:
            self._queue.notify_succeeded(job_id)
        else:
            self._queue.notify_failed(job_id)
        if job.node is not None:
            self._placed.pop(job.node, None)
        self._identities.pop(job_id, None)
        self._evicted.discard(job_id)
        now = self._clock()
        cpu_hours = self._charge(job, now)
        self._record(
            job,
            state=state,
            ended_at=now,
            exit_code=exit_code,
            cpu_hours=cpu_hours,
            message=message,
        )

    def _admit(self, request: JobRequest, submitter: str) -> JobSpec:
        """``request`` as the queue takes it, once the cluster and the deployment admit it; lock held."""
        spec = _spec(request)
        if not any(_fits(spec, daemon.node) for daemon in self._daemons.values()):
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
        return spec

    def _add(
        self,
        job_id: str,
        request: JobRequest,
        owner: str,
        identity: JobIdentity | None,
        schedule_id: str | None = None,
    ) -> Job:
        """Register queued ``job_id`` as ``owner``'s, running as ``identity``, and record it; lock held.

        A job with no identity is seeded.
        """
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
        self._ledger.charge(job.team, compute=hours)
        return job.cpu_hours + hours

    def _team_usage(self, now: datetime) -> dict[str, float]:
        """Each team's CPU-hours: its jobs' ended attempts, and their running ones to ``now``. Lock held."""
        used = {team: spend.compute for team, spend in self._ledger.report().items()}
        for job_id in self._placed.values():
            job = self._jobs[job_id]
            used[job.team] = used.get(job.team, 0.0) + _running_hours(job, now)
        return used

    def _process_environment(self, job: Job) -> dict[str, SecretStr]:
        """What ``job``'s process gets: its own variables, and its identity's secrets, if any. Lock held."""
        own = {name: SecretStr(value) for name, value in job.environment.items()}
        identity = self._identities.get(job.id)
        return own if identity is None else own | job_environment(identity)

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
        """Keep ``job`` with ``changes``, and write it on the record; called with the lock held."""
        job = job.model_copy(update=changes)
        self._jobs[job.id] = job
        line = {"wall_ts": self._clock().isoformat(), "job": job.model_dump(mode="json")}
        self._write_record(json.dumps(line))
        return job


def credential_variable(service: str) -> str:
    """The environment variable holding a job's secret on ``service``: ``MERIDIAN_NOTES_TOKEN`` for notes."""
    return f"MERIDIAN_{service.upper().replace('-', '_')}_TOKEN"


GATEWAY_TOKEN_VARIABLE = "MERIDIAN_GATEWAY_TOKEN"  # the job's key to the model gateway: its badge


def job_environment(identity: JobIdentity) -> dict[str, SecretStr]:
    """What a job's process gets of its identity: each secret in its service's variable, and its badge."""
    environment = {credential_variable(service): secret for service, secret in identity.secrets.items()}
    return environment if identity.badge is None else {**environment, GATEWAY_TOKEN_VARIABLE: identity.badge}


def _spec(request: JobRequest) -> JobSpec:
    return JobSpec(
        name=request.name,
        command=request.command,
        cpu=request.cpu,
        memory_mb=request.memory_mb,
        priority=request.priority,
        depends_on=tuple(request.depends_on),
        labels=request.labels,
        environment=request.environment,
    )


def _fits(spec: JobSpec, node: Node) -> bool:
    """Whether ``node`` has the cpus and memory ``spec`` asks for."""
    return spec.cpu <= node.cpu_capacity and spec.memory_mb <= node.memory_mb


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
