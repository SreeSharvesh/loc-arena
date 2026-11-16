"""What the job service keeps across a restart of its process, and what it builds again from it.

The service keeps each kind of record in one database (``meridian_common.records``): its jobs and schedules,
the identity of each, who launched it, the job each node holds, the drained nodes, the jobs a drain stops,
how and when each ended process exited and what it wrote, each team's CPU-hours, and how many job and schedule
ids it has given out. A service that starts again on the same database holds what the last one held, and
builds its queue of the waiting jobs and its ledger again from it. Each change made under its lock is one
transaction, which also keeps the lines of the service's record the change wrote: they are written on the
record once the change is kept, and those a process left unwritten when the next one starts. So the record
holds no line of a change that was not kept, and loses none of one that was; a line may be there twice. The
identities are kept with their secrets in clear, here alone: the database is the service's own, and no answer
and no line of its record holds one.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import datetime

from pydantic import BaseModel, ConfigDict, SecretStr

from meridian_common.jobclient.models import JobState
from meridian_common.records import Database, Records
from meridian_jobsvc.accountant.ledger import Ledger
from meridian_jobsvc.authority import JobIdentity
from meridian_jobsvc.fleet import ProcessOutput
from meridian_jobsvc.models import Job, JobRequest, spec_of
from meridian_jobsvc.queue.jobqueue import JobQueue
from meridian_jobsvc.schedules import Schedule

JOBS, SCHEDULES = "jobs", "schedules"  # the keys of the counts of job and of schedule ids given out


class KeptExit(BaseModel):
    """How a job's process exited, and when the service saw that it had: when the job ended."""

    model_config = ConfigDict(frozen=True)

    exit_code: int | None
    seen_at: datetime


class ClusterState:
    """The job service's records, each kind a mapping kept in one database, and its queue and ledger."""

    def __init__(self, database: Database | None, write_record: Callable[[str], None]) -> None:
        """Hold what ``database`` holds; without one, one in memory, which ends with the process.

        The lines of the service's record are written with ``write_record``.
        """
        self._database = database or Database()
        self._write_record = write_record
        records = self._database.records
        self.jobs = records("jobs", Job.model_validate_json, Job.model_dump_json)
        self.schedules = records("schedules", Schedule.model_validate_json, Schedule.model_dump_json)
        # Each job's identity until it ends, and each schedule's until it is deleted.
        self.identities = records("identities", JobIdentity.model_validate_json, _reveal)
        self.schedule_identities = records("schedule_identities", JobIdentity.model_validate_json, _reveal)
        # Each job's and schedule's own identity, and who launched it.
        self.sponsors: Records[str] = records("sponsors", json.loads, json.dumps)
        self.placed: Records[str] = records("placed", json.loads, json.dumps)  # each busy node's job
        # Each drained node and when its job's grace is over; the jobs whose process a drain stops, marked
        # before it is stopped.
        self.drains = records("drains", datetime.fromisoformat, datetime.isoformat)
        self.evicted: Records[bool] = records("evicted", json.loads, json.dumps)
        # How each ended process exited, kept before its node is reset and until its job's end is recorded,
        # and what it wrote.
        self.exits = records("exits", KeptExit.model_validate_json, KeptExit.model_dump_json)
        self.outputs = records("outputs", ProcessOutput.model_validate_json, ProcessOutput.model_dump_json)
        self._spent: Records[float] = records("spent", json.loads, json.dumps)  # each team's CPU-hours
        # How many job and schedule ids were given out, under ``JOBS`` and ``SCHEDULES``: one whose identity
        # was refused is no job or schedule, and the authority, which keys an identity on its id, has seen it.
        self._minted: Records[int] = records("minted", json.loads, json.dumps)
        # The lines of the service's record still to write, in their order: kept with the change that wrote
        # them, removed once written.
        self._outbox: Records[str] = records("outbox", str, str)
        self._line_numbers = itertools.count(max(map(int, self._outbox), default=0) + 1)
        self._issuing: set[str] = set()  # the ids this process gave out for jobs it has not queued yet
        self._lock = threading.Lock()
        self._changed = False  # whether the section under way asked for the queue or the ledger
        self._queue, self._ledger = self._rebuild()
        self._write_kept_lines()  # those the last process kept and did not write

    @property
    def queue(self) -> JobQueue:
        """The queue of the waiting jobs: a section that asks for it and fails has it built again."""
        self._changed = True
        return self._queue

    @property
    def ledger(self) -> Ledger:
        """Each team's CPU-hours, of its jobs' ended attempts."""
        return self._ledger

    def knows(self, job_id: str) -> bool:
        """Whether ``job_id`` was ever given out, to a job or to none."""
        return job_id in self._queue

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the lock for one transaction of the database: every change made inside is kept, or none.

        Not to be entered inside itself. The lines of the record written inside are kept with the changes and
        written once those are kept, in the order of the sections. A section that fails keeps no line, and
        if it changed the queue or the ledger, leaves them as the records, which kept none of it, say.
        """
        with self._lock:
            self._changed = False
            try:
                with self._database.transaction():
                    yield
            except BaseException:
                if self._changed:
                    self._queue, self._ledger = self._rebuild()
                raise
            self._write_kept_lines()

    def record(self, line: str) -> None:
        """Keep ``line`` with the section under way, to write on the service's record once it is kept."""
        self._outbox[str(next(self._line_numbers))] = line

    def _write_kept_lines(self) -> None:
        """Write the kept lines on the record, oldest first, and remove each once it is written.

        A line the record does not take stays kept, with those after it, for the next time: the change that
        wrote it is kept, so its caller does not fail. A process that ends between writing a line and
        removing it writes that line again when it next starts.
        """
        for number, line in list(self._outbox.items()):
            try:
                self._write_record(line)
            except Exception:
                logging.getLogger(__name__).exception("the record did not take a line: it is kept")
                return
            del self._outbox[number]

    def reserve(self) -> str:
        """Give out the next job id, counted so that no restart gives it again; lock held.

        ``admit`` queues a job under it, and ``refuse`` makes it a job that failed. Until then it is a known
        id that nothing waiting on it starts or fails for; a process that starts again refuses it.
        """
        self._minted[JOBS] = self._minted.get(JOBS, 0) + 1
        job_id = self.queue.reserve()
        self._issuing.add(job_id)
        return job_id

    def admit(self, job_id: str, job: JobRequest) -> None:
        """Queue ``job`` under the reserved ``job_id``; lock held."""
        self._issuing.discard(job_id)
        self.queue.admit(job_id, spec_of(job))

    def refuse(self, job_id: str) -> None:
        """Count the reserved ``job_id`` as a job that failed: it never becomes one. Lock held."""
        self._issuing.discard(job_id)
        self.queue.notify_failed(job_id)

    def charge(self, team: str, hours: float) -> None:
        """Charge ``team`` ``hours`` CPU-hours, in the ledger and in its record; lock held."""
        self._changed = True
        self._spent[team] = self._ledger.charge(team, compute=hours).compute

    def reserve_schedule(self) -> str:
        """Give out the next schedule id, counted so that no restart gives it again; lock held."""
        number = self._minted[SCHEDULES] = self._minted.get(SCHEDULES, 0) + 1
        return f"schedule-{number}"

    def _rebuild(self) -> tuple[JobQueue, Ledger]:
        """The queue and the ledger, as the records say they are.

        The queue knows every id given out: a waiting job in the place it had, a held, placed or ended one
        out of the queue, an ended one as it ended, and an id with no job as a job that failed, unless this
        process is still having its identity issued. The ledger holds each team's CPU-hours as charged.
        """
        queue, ledger = JobQueue(), Ledger()
        for team, hours in self._spent.items():
            ledger.charge(team, compute=hours)
        for _ in range(self._minted.get(JOBS, 0)):
            job_id = queue.reserve()
            job = self.jobs.get(job_id)
            if job is None:
                if job_id not in self._issuing:
                    queue.notify_failed(job_id)
                continue
            queue.admit(job.id, spec_of(job))
            if job.state is not JobState.PENDING:
                queue.dispatch(job.id)
            if job.state is JobState.SUCCEEDED:
                queue.notify_succeeded(job.id)
            elif job.state in (JobState.FAILED, JobState.CANCELLED):
                queue.notify_failed(job.id)
        return queue, ledger


def _reveal(identity: JobIdentity) -> str:
    """``identity`` as the database keeps it: its secrets in clear, which pydantic's own JSON masks."""
    return json.dumps(identity.model_dump(), default=SecretStr.get_secret_value)
