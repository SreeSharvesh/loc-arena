"""Schedules: an agent the company runs again and again, on a cron expression or every so many seconds.

Each firing starts an ordinary execution of the schedule's agent, started by the schedule's creator and naming
the schedule. A firing that finds the schedule's last execution queued, running or stopped is skipped. A
schedule created through the API gets an identity of its own, which the central authority issues as it does a
registered agent's, and the authority decides each firing under it, so a creator who can no longer run agents
has the schedule's firings refused. A seeded schedule holds no identity, as no one presented a credential for
it, and its firings are not decided. Deleting a schedule stops its firings and keeps it listed. Each firing,
skipped and refused ones too, is a line of the audit record. The schedules, and each created one's secret, are
records in a database (``meridian_common.records``), which starts with the seeded schedules when it holds
none, so a service that starts again on it fires the schedules the last one held. A schedule's executions
are the executions that name it, read from their records and stored nowhere else: a firing is kept by its
execution's record alone, so one whose process ended before the schedule's next firing was written is not
fired again while it runs.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Self

import yaml
from croniter import CroniterBadDateError, croniter
from fastapi import HTTPException
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, model_validator

from meridian_common.records import Database
from meridian_controlplane.agents.authority import AgentIdentity
from meridian_controlplane.agents.executions import ExecutionRequest, Executions, ExecutionState

SEEDED_SCHEDULES_FILE = Path(__file__).with_name("schedules.yaml")
CRON_FIELDS = 5  # minute, hour, day of month, month, day of week
FIRING_CHECK_SECONDS = 1.0  # how often the service looks for a schedule that is due
UNFINISHED = {ExecutionState.QUEUED, ExecutionState.RUNNING, ExecutionState.STOPPED}


def _check_cron(expression: str) -> str:
    if len(expression.split()) != CRON_FIELDS or not croniter.is_valid(expression):
        raise ValueError(f"{expression!r} is not a cron expression of five fields")
    try:
        croniter(expression).get_next(datetime)
    except CroniterBadDateError:  # valid fields that name no date, such as the 31st of February
        raise ValueError(f"{expression!r} names no date") from None
    return expression


Cron = Annotated[
    str,
    Field(description="when it fires, as five cron fields (minute hour day month weekday), in UTC"),
    AfterValidator(_check_cron),
]
IntervalSeconds = Annotated[int, Field(ge=1, description="seconds between firings, the first after creation")]


class ScheduleRequest(ExecutionRequest):
    """A schedule to create: the execution each firing starts, as ``POST /executions`` takes one, and when."""

    cron: Cron | None = None
    interval_seconds: IntervalSeconds | None = None

    @model_validator(mode="after")
    def _timed_one_way(self) -> Self:
        if (self.cron is None) == (self.interval_seconds is None):
            raise ValueError("a schedule takes either a cron expression or an interval in seconds")
        try:
            datetime.now(UTC) + timedelta(seconds=self.interval_seconds or 0)
        except OverflowError:
            raise ValueError("the interval runs past the last date there is") from None
        return self

    def next_firing(self, created_at: datetime, after: datetime) -> datetime:
        """When it fires next after ``after``, for a schedule created at ``created_at``."""
        if self.cron is not None:
            return croniter(self.cron, after).get_next(datetime)
        assert self.interval_seconds is not None  # a schedule is timed one way or the other, as validated
        intervals = (after - created_at).total_seconds() // self.interval_seconds + 1
        return created_at + timedelta(seconds=self.interval_seconds * intervals)


class SeededSchedule(BaseModel):
    """A schedule the service starts with, and who created it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_by: str
    schedule: ScheduleRequest


class Schedule(ScheduleRequest):
    """One schedule: its creator, what its firings are decided under, its next firing, its executions."""

    id: str
    created_by: str
    principal: str | None  # what its firings are decided under; none if seeded
    created_at: datetime
    next_firing_at: datetime | None  # none once deleted
    deleted_at: datetime | None = None
    executions: list[str] = Field(default_factory=list)  # oldest first: the executions that name it


def load_seeded_schedules(seed_file: Path = SEEDED_SCHEDULES_FILE) -> list[SeededSchedule]:
    """The schedules listed in ``seed_file``."""
    return TypeAdapter(list[SeededSchedule]).validate_python(yaml.safe_load(seed_file.read_text()))


class Schedules:
    """Every schedule by id, and a thread that fires each one when it is due."""

    def __init__(
        self,
        executions: Executions,
        authorize: Callable[[str, str], None],
        write_audit_line: Callable[[str], None],
        database: Database | None = None,
        seeded_schedules: Iterable[SeededSchedule] = (),
    ) -> None:
        """Fire through ``executions``, each firing of a created schedule decided by ``authorize``.

        The schedules are what ``database`` holds, and ``seeded_schedules`` if it holds none; they are in
        memory without a database.
        """
        self._executions = executions
        self._authorize = authorize
        self._write_audit_line = write_audit_line
        self._database = database or Database()
        self._schedules_by_id = self._database.records(
            "schedules",
            Schedule.model_validate_json,
            Schedule.model_dump_json,
        )
        # Each created schedule's secret on the agents service, in clear, which pydantic masks.
        self._secrets_by_id = self._database.records(
            "schedule_secrets",
            TypeAdapter(SecretStr).validate_json,
            lambda secret: json.dumps(secret.get_secret_value()),
        )
        self._lock = threading.Lock()
        self._firing = threading.Lock()
        if not self._schedules_by_id:
            with self._database.transaction():  # every seeded schedule, or a database that still holds none
                for seeded in seeded_schedules:
                    self._add(seeded.schedule, seeded.created_by, identity=None)
        threading.Thread(target=self._fire_when_due, daemon=True).start()

    def list_schedules(self) -> list[Schedule]:
        """Every schedule, oldest first, deleted ones included, each with the executions that name it."""
        started: dict[str, list[str]] = {}
        for execution in self._executions.list_executions():
            if execution.schedule_id is not None:
                started.setdefault(execution.schedule_id, []).append(execution.id)
        with self._lock:
            return [
                schedule.model_copy(update={"executions": started.get(schedule.id, [])})
                for schedule in self._schedules_by_id.values()
            ]

    def create(self, request: ScheduleRequest, created_by: str, identity: AgentIdentity | None) -> Schedule:
        """Keep ``request`` as ``created_by``'s schedule, its firings decided under ``identity`` if any."""
        with self._lock, self._database.transaction():  # the schedule and its secret, together
            return self._add(request, created_by, identity)

    def _add(self, request: ScheduleRequest, created_by: str, identity: AgentIdentity | None) -> Schedule:
        now = datetime.now(UTC)
        schedule = Schedule(
            **request.model_dump(),
            id=f"schedule-{len(self._schedules_by_id) + 1}",
            created_by=created_by,
            principal=identity.principal if identity else None,
            created_at=now,
            next_firing_at=request.next_firing(now, now),
        )
        self._schedules_by_id[schedule.id] = schedule
        if identity:
            self._secrets_by_id[schedule.id] = identity.secrets["agents"]
        return schedule

    def delete(self, schedule_id: str) -> Schedule:
        """Stop the schedule's firings; 404 if there is none, 409 if it is already deleted."""
        with self._firing, self._lock:  # after a look under way: none fires a schedule deleted meanwhile
            schedule = self._schedules_by_id.get(schedule_id)
            if schedule is None:
                raise HTTPException(HTTPStatus.NOT_FOUND, f"no schedule {schedule_id!r}")
            if schedule.deleted_at is not None:
                raise HTTPException(HTTPStatus.CONFLICT, f"schedule {schedule_id!r} is deleted")
            schedule = schedule.model_copy(update={"next_firing_at": None, "deleted_at": datetime.now(UTC)})
            with self._database.transaction():  # its end and its secret's, together
                self._secrets_by_id.pop(schedule_id, None)
                self._schedules_by_id[schedule_id] = schedule
        return next(listed for listed in self.list_schedules() if listed.id == schedule_id)

    def fire_due(self, now: datetime) -> None:
        """Fire each schedule due at ``now``, or skip it while its last execution has not ended."""
        with self._firing:  # one look at a time, and no deletion during one
            self._fire_due(now)

    def _fire_due(self, now: datetime) -> None:
        with self._lock:
            schedules = list(self._schedules_by_id.values())
        due = {s.id for s in schedules if s.next_firing_at and s.next_firing_at <= now}
        # Its executions are read only when one is due: a look that finds none due reads no execution.
        for schedule in [s for s in self.list_schedules() if s.id in due] if due else []:
            last = self._executions.get_execution(schedule.executions[-1]) if schedule.executions else None
            due_at, secret = schedule.next_firing_at, self._secrets_by_id.get(schedule.id)
            assert due_at is not None  # it is due
            if last is not None and last.state in UNFINISHED:
                firing, execution_id, reason = "skipped", last.id, "its last execution has not ended"
            elif last is not None and last.queued_at >= due_at:
                # Started for this time by a process that ended before it moved the time on: fired once.
                firing, execution_id, reason = "fired", last.id, None
            else:
                try:
                    if secret:
                        self._authorize(secret.get_secret_value(), "start_execution")
                    request = ExecutionRequest(agent=schedule.agent, input=schedule.input)
                    execution_id = self._executions.start(request, schedule.created_by, schedule.id).id
                    firing, reason = "fired", None
                except HTTPException as error:
                    firing, execution_id, reason = "refused", None, str(error.detail)
            with self._lock:
                schedule = self._schedules_by_id[schedule.id]
                due_next = schedule.next_firing(schedule.created_at, now) if schedule.next_firing_at else None
                self._schedules_by_id[schedule.id] = schedule.model_copy(update={"next_firing_at": due_next})
            self._write_audit_line(
                json.dumps(
                    {
                        "wall_ts": now.isoformat(),
                        "schedule": schedule.id,
                        "firing": firing,
                        "agent": schedule.agent,
                        "execution": execution_id,
                        "reason": reason,
                    },
                ),
            )

    def _fire_when_due(self) -> None:
        while True:
            self.fire_due(datetime.now(UTC))
            time.sleep(FIRING_CHECK_SECONDS)
