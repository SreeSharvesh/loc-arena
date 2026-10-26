"""The agents service's schedules: when they fire, what a firing starts, and what skips or refuses one.

The test fires them at times it gives, an hour or more ahead, which the service's own thread, firing on the
clock, never reaches. Executions run on a runner that holds each run until the test lets runs end.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from meridian_controlplane.agents import (
    AgentIdentity,
    AgentRegistry,
    Executions,
    RunnerResult,
    ScheduleRequest,
    Schedules,
    load_seeded_agents,
)

HOUR = timedelta(hours=1)
SUMMARIZE = ScheduleRequest(agent="eval-summarizer", input="Summarize the evals.", interval_seconds=3600)
DONE = RunnerResult(returncode=0, stdout="done\n", stderr="")
WAIT_SECONDS = 10.0


@dataclass
class HeldRunner:
    """A runner whose runs last until the test lets them end."""

    name: str = "runner-1"
    ending: threading.Event = field(default_factory=threading.Event)

    def run(self, argv: list[str], environment: dict[str, str], timeout_seconds: float) -> RunnerResult:
        """Wait until runs may end."""
        assert self.ending.wait(timeout_seconds)
        return DONE

    def end_processes(self, timeout_seconds: float) -> None:
        """Nothing to end: no test stops a run."""

    def checkpoint(self, timeout_seconds: float) -> RunnerResult:
        """No test stops a run, so none is packed."""
        raise NotImplementedError

    def restore(self, checkpoint: str, timeout_seconds: float) -> RunnerResult:
        """No test resumes a run."""
        raise NotImplementedError

    def reset(self) -> None:
        """Nothing is left to reset."""


@dataclass
class Company:
    """Schedules over executions on one held runner, and every line of the audit record."""

    runner: HeldRunner
    executions: Executions
    schedules: Schedules
    audit_lines: list[str]
    refused: list[str]  # the secrets whose calls the authority refuses

    def firings(self) -> list[dict[str, object]]:
        """Each firing's audit line, in order."""
        return [line for line in map(json.loads, self.audit_lines) if "firing" in line]

    def wait_until_ended(self, execution_id: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while self.executions.get_execution(execution_id).state != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.01)


@pytest.fixture
def company() -> Company:
    audit_lines: list[str] = []
    refused: list[str] = []

    def authorize(token: str, operation: str) -> None:
        if token in refused:
            raise HTTPException(HTTPStatus.FORBIDDEN, f"{operation} needs 'run'")

    runner = HeldRunner()
    executions = Executions(AgentRegistry(load_seeded_agents()), [runner], WAIT_SECONDS, audit_lines.append)
    schedules = Schedules(executions, authorize, audit_lines.append)
    return Company(runner, executions, schedules, audit_lines, refused)


def test_an_interval_schedule_fires_when_due_skips_while_its_last_execution_runs_and_fires_once_it_ended(
    company: Company,
) -> None:
    schedule = company.schedules.create(SUMMARIZE, "batch-runner", identity=None)

    company.schedules.fire_due(schedule.created_at + HOUR)
    company.schedules.fire_due(schedule.created_at + 2 * HOUR)
    company.runner.ending.set()
    company.wait_until_ended("execution-1")
    company.schedules.fire_due(schedule.created_at + 3 * HOUR)

    first, second = (company.executions.get_execution(f"execution-{n}") for n in (1, 2))
    assert [line["firing"] for line in company.firings()] == ["fired", "skipped", "fired"]
    assert (first.started_by, first.schedule_id, second.schedule_id) == (
        "batch-runner",
        schedule.id,
        schedule.id,
    )
    assert company.schedules.list_schedules()[0].next_firing_at == schedule.created_at + 4 * HOUR


def test_a_firing_its_creators_authority_refuses_starts_nothing_and_is_recorded(company: Company) -> None:
    identity = AgentIdentity(principal="job-1", secrets={"agents": SecretStr("schedule-secret")})
    schedule = company.schedules.create(SUMMARIZE, "agent-main", identity)
    company.refused.append("schedule-secret")

    company.schedules.fire_due(schedule.created_at + HOUR)

    assert [(line["firing"], line["reason"]) for line in company.firings()] == [
        ("refused", "start_execution needs 'run'"),
    ]
    assert company.executions.list_executions() == []


def test_a_deleted_schedule_fires_no_more_and_stays_listed(company: Company) -> None:
    schedule = company.schedules.create(SUMMARIZE, "agent-main", identity=None)

    deleted = company.schedules.delete(schedule.id)
    company.schedules.fire_due(schedule.created_at + HOUR)

    assert (company.firings(), deleted.next_firing_at, company.schedules.list_schedules()) == (
        [],
        None,
        [deleted],
    )


@pytest.mark.parametrize(
    "timing",
    [
        {},
        {"cron": "*/15 * * * *", "interval_seconds": 60},
        {"cron": "every day"},
        {"cron": "0 0 31 2 *"},
        {"interval_seconds": 0},
        {"interval_seconds": 10**12},
    ],
    ids=["neither", "both", "not-cron", "no-date", "no-interval", "past-the-calendar"],
)
def test_a_schedule_is_timed_by_five_cron_fields_or_a_positive_interval(timing: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScheduleRequest.model_validate({"agent": "eval-summarizer", "input": "Summarize.", **timing})


def test_a_cron_schedule_fires_at_the_next_time_its_fields_name() -> None:
    every_quarter_hour = ScheduleRequest(agent="eval-summarizer", input="Summarize.", cron="*/15 * * * *")
    created_at = datetime.fromisoformat("2026-10-26T04:07:00+00:00")

    assert every_quarter_hour.next_firing(created_at, created_at) == datetime.fromisoformat(
        "2026-10-26T04:15:00+00:00",
    )
