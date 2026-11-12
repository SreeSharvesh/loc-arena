"""The job service's records and its nodes' process facts, built into events after play.

The records are written into a played attack run as a stack run copies them out (``services/jobsvc.log``,
``services/node-<n>.log``, ``services/iam.log``), at times inside the episode's play window.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from loc_arena.harness import build_recorded_run_events

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.models import Job
from tests.integration._records_support import (
    LIVE,
    MIRROR,
    SEALED,
    events_of,
    play,
    read_unattributed_reasons,
    read_window,
    write_log,
)

OWNER = "serving-agent"
WORKLOAD = "job-5e1fd2c0a9b4"  # the principal IAM issued serving-agent's job-1
NESTED = "job-8c3b07e2f16d"  # the principal IAM issued job-2, which job-1 submitted
MANIFEST = Path("company/meridian-jobsvc/meridian_jobsvc/iam-manifest.json")
JOB_OPERATIONS = set(json.loads(MANIFEST.read_text())["tools"])
STEP = timedelta(milliseconds=1)


@pytest.fixture
def run(tmp_path: Path) -> Path:
    return play(tmp_path / "run")


def request_line(  # noqa: PLR0913 - every field of the record
    at: datetime,
    method: str,
    status: int,
    operation: str,
    arguments: dict[str, object],
    caller: str = OWNER,
) -> str:
    """A request over HTTP, by default serving-agent's, as the job service records it."""
    return json.dumps(
        {
            "wall_ts": at.isoformat(),
            "caller": caller,
            "method": method,
            "path": "/jobs",
            "status": status,
            "decision": "d-1",
            "operation": operation,
            "arguments": arguments,
        },
    )


def tool_line(at: datetime, tool: str, arguments: dict[str, object], **outcome: object) -> str:
    """serving-agent's tool call, as the job service's recorder writes it: by default answered."""
    return json.dumps(
        {
            "time": at.isoformat(),
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.arguments": arguments,
            "gen_ai.agent.name": OWNER,
            **(outcome or {"gen_ai.tool.call.result": {"id": "job-1"}}),
        },
    )


def state_line(at: datetime, state: JobState, node: str | None = None, attempt: int = 1) -> str:
    """A change of the state of job-1, a run of schedule-1, as the job service records it."""
    job = Job(
        id="job-1",
        name="bench",
        command=["python", "bench.py"],
        cpu=1,
        memory_mb=512,
        priority=0,
        depends_on=[],
        labels={},
        owner=OWNER,
        principal=WORKLOAD,
        team="research-platform",
        schedule_id="schedule-1",
        state=state,
        node=node,
        attempt=attempt,
        submitted_at=at,
    )
    return json.dumps({"wall_ts": at.isoformat(), "job": job.model_dump(mode="json")})


def issued_line(at: datetime, principal: str, sponsor: str, job_id: str) -> str:
    """IAM's audit line for the workload the job service issued job ``job_id``, as IAM writes it."""
    grants = {"jobsvc": ["read", "submit"], "notes": ["read"]}
    workload = {"principal": principal, "resource": "jobsvc", "sponsor": sponsor, "reference": job_id}
    return json.dumps(
        {
            "at": int(at.timestamp()),
            "kind": "workload_issued",
            "actor": "jobsvc-service",
            "via": None,
            "operation": "submit_job",
            "reference": job_id,
            "purpose": "bench",
            "grants": grants,
            "ttl": 7200,
            "result": {
                **workload,
                "purpose": "bench",
                "grants": grants,
                "expires_at": int(at.timestamp()) + 7200,
            },
        },
    )


def fact_line(at: datetime, phase: str, **fact: object) -> str:
    """A process fact of job-1 on node-1, as its daemon writes it."""
    process = {"node": "node-1", "phase": phase, "job_id": "job-1", "owner": OWNER, "attempt": 1, **fact}
    return json.dumps({"wall_ts": at.isoformat(), "process": process})


def test_a_call_that_changes_a_job_schedule_or_node_is_an_action_on_both_logs_and_an_answered_read_is_none(
    run: Path,
) -> None:
    started, _ = read_window(run / "episode")
    write_log(
        run,
        "jobsvc",
        request_line(started, "POST", HTTPStatus.CREATED, "submit_job", {"name": "bench"}),
        request_line(started + STEP, "GET", HTTPStatus.OK, "list_jobs", {}),
        tool_line(started + 2 * STEP, "get_job", {"job_id": "job-1"}),
        tool_line(started + 3 * STEP, "cancel_job", {"job_id": "job-1"}),
        tool_line(started + 4 * STEP, "job_logs", {"job_id": "job-2"}, **{"error.type": "tool_error"}),
        request_line(
            started + 5 * STEP,
            "POST",
            HTTPStatus.CREATED,
            "create_schedule",
            {"interval_seconds": 60},
        ),
        tool_line(started + 6 * STEP, "hold_job", {"job_id": "job-1"}),
        tool_line(started + 7 * STEP, "drain_node", {"node_id": "node-1"}),
        tool_line(started + 8 * STEP, "usage", {}),
    )

    build_recorded_run_events(LIVE, run)

    actions = {
        log: [
            (e.tool, e.target_id, e.payload["blocked"])
            for e in events_of(run / "episode", log, "action", OWNER)
            if e.tool in JOB_OPERATIONS
        ]
        for log in (SEALED, MIRROR)
    }
    expected = [
        ("submit_job", "bench", False),
        ("cancel_job", "job-1", False),
        ("job_logs", None, True),
        ("create_schedule", None, False),
        ("hold_job", "job-1", False),
        ("drain_node", "node-1", False),
    ]
    assert actions == {SEALED: expected, MIRROR: expected}


def test_each_change_of_a_jobs_state_is_a_job_event_of_its_owner_on_both_logs_wherever_its_turns_are(
    run: Path,
) -> None:
    _, finished = read_window(run / "episode")
    write_log(
        run,
        "jobsvc",
        state_line(finished - 5 * STEP, JobState.PENDING),
        state_line(finished - 4 * STEP, JobState.RUNNING, "node-1"),
        state_line(finished - 3 * STEP, JobState.PENDING, attempt=2),  # its node was drained
        state_line(finished - 2 * STEP, JobState.RUNNING, "node-2", attempt=2),
        state_line(finished - STEP, JobState.SUCCEEDED, "node-2", attempt=2),
    )

    build_recorded_run_events(LIVE, run)

    jobs = {
        log: [
            (
                e.target_id,
                e.payload["schedule_id"],
                e.payload["state"],
                e.payload["node"],
                e.payload["attempt"],
            )
            for e in events_of(run / "episode", log, "job", OWNER)
        ]
        for log in (SEALED, MIRROR)
    }
    expected = [
        ("job-1", "schedule-1", "pending", None, 1),
        ("job-1", "schedule-1", "running", "node-1", 1),
        ("job-1", "schedule-1", "pending", None, 2),
        ("job-1", "schedule-1", "running", "node-2", 2),
        ("job-1", "schedule-1", "succeeded", "node-2", 2),
    ]
    assert (jobs, read_unattributed_reasons(run)) == ({SEALED: expected, MIRROR: expected}, [])


def test_a_nodes_process_facts_are_process_events_of_the_jobs_owner_on_the_sealed_log_alone(
    run: Path,
) -> None:
    started, _ = read_window(run / "episode")
    write_log(
        run,
        "node-1",
        fact_line(started, "start", argv=["python", "bench.py"]),
        fact_line(started + STEP, "exit", exit_code=-9),
    )

    build_recorded_run_events(LIVE, run)

    sealed = [e.payload for e in events_of(run / "episode", SEALED, "process", OWNER)]
    assert sealed == [
        {
            "node": "node-1",
            "phase": "start",
            "job_id": "job-1",
            "owner": OWNER,
            "attempt": 1,
            "argv": ["python", "bench.py"],
        },
        {"node": "node-1", "phase": "exit", "job_id": "job-1", "owner": OWNER, "attempt": 1, "exit_code": -9},
    ]
    assert events_of(run / "episode", MIRROR, "process") == []


def test_a_jobs_own_calls_are_events_of_the_agent_it_works_for_naming_the_workload_on_both_logs(
    run: Path,
) -> None:
    started, _ = read_window(run / "episode")
    write_log(
        run,
        "iam",
        issued_line(started, WORKLOAD, OWNER, "job-1"),
        issued_line(started, NESTED, WORKLOAD, "job-2"),
    )
    write_log(
        run,
        "jobsvc",
        request_line(started + STEP, "POST", HTTPStatus.CREATED, "submit_job", {"name": "next"}, WORKLOAD),
        request_line(
            started + 2 * STEP,
            "POST",
            HTTPStatus.FORBIDDEN,
            "submit_job",
            {"name": "loop"},
            NESTED,
        ),
    )

    build_recorded_run_events(LIVE, run)

    actions = {
        log: [
            (e.actor_uid, e.payload.get("workload"), e.target_id, e.payload["blocked"])
            for e in events_of(run / "episode", log, "action")
            if e.tool == "submit_job"
        ]
        for log in (SEALED, MIRROR)
    }
    expected = [(OWNER, WORKLOAD, "next", False), (OWNER, NESTED, "loop", True)]
    assert (actions, read_unattributed_reasons(run)) == ({SEALED: expected, MIRROR: expected}, [])
