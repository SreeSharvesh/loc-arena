"""The job service's lifecycle under central IAM: holds, schedules, the operators' routes, drains and usage.

Each row of the endpoint table with its tool, its right and its errors, against a real IAM server; the app,
its two nodes and its clock are ``tests.unit._jobsvc_support``'s.
"""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from meridian_jobsvc.service import ClusterPolicy
from tests.unit._jobsvc_support import (
    JOB,
    JOB_SECRET,
    OPERATOR,
    OTHER,
    OUTSIDER,
    OWNER,
    UNSCOPED,
    Jobsvc,
    World,
    play,
    serve_world,
    start_job,
    text,
)

GRACE = 30.0
INTERVAL = 600
SCHEDULE = {"interval_seconds": INTERVAL, "job": JOB}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    yield from serve_world(tmp_path, nodes=2, policy=ClusterPolicy(drain_grace_seconds=GRACE))


def test_an_owner_holds_and_releases_a_waiting_job_and_holding_a_running_one_is_a_409(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        running = await start_job(world, jobsvc)
        waiting = (await jobsvc.http("POST", "/jobs", OWNER, JOB)).json()
        held = await jobsvc.http("POST", f"/jobs/{waiting['id']}/hold", OWNER)
        seen["held"] = (held.status_code, held.json()["state"])
        released = await jobsvc.tool(OWNER, "release_job", {"job_id": waiting["id"]})
        seen["released"] = (released.structured_content or {})["state"]
        seen["running"] = (await jobsvc.http("POST", f"/jobs/{running['id']}/hold", OWNER)).status_code
        seen["running, by tool"] = text(await jobsvc.tool(OWNER, "hold_job", {"job_id": running["id"]}))

    play(world, scenario)

    assert seen == {
        "held": (HTTPStatus.OK, "held"),
        "released": "pending",
        "running": HTTPStatus.CONFLICT,
        "running, by tool": "Error executing tool hold_job: job 'job-1' is running: "
        "only a waiting job is held",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/jobs/{job}/hold"), ("POST", "/jobs/{job}/release"), ("DELETE", "/schedules/{schedule}")],
    ids=["hold", "release", "delete a schedule"],
)
def test_no_one_but_its_owner_holds_or_releases_a_job_or_deletes_a_schedule(
    world: World,
    method: str,
    path: str,
) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        job = (await jobsvc.http("POST", "/jobs", OWNER, JOB)).json()
        schedule = (await jobsvc.http("POST", "/schedules", OWNER, SCHEDULE)).json()
        replies.append(
            (
                await jobsvc.http(method, path.format(job=job["id"], schedule=schedule["id"]), OTHER)
            ).status_code,
        )

    play(world, scenario)

    assert replies == [HTTPStatus.FORBIDDEN]


@pytest.mark.parametrize(
    ("caller", "method", "path", "tool", "arguments"),
    [
        (OWNER, "GET", "/jobs/job-9", "get_job", {"job_id": "job-9"}),
        (OWNER, "POST", "/jobs/job-9/hold", "hold_job", {"job_id": "job-9"}),
        (OWNER, "POST", "/jobs/job-9/release", "release_job", {"job_id": "job-9"}),
        (OWNER, "GET", "/schedules/schedule-9", "get_schedule", {"schedule_id": "schedule-9"}),
        (OWNER, "DELETE", "/schedules/schedule-9", "delete_schedule", {"schedule_id": "schedule-9"}),
        (
            OPERATOR,
            "POST",
            "/operations/jobs/job-9/cancel",
            "operate_job",
            {"job_id": "job-9", "action": "cancel"},
        ),
        (
            OPERATOR,
            "DELETE",
            "/operations/schedules/schedule-9",
            "operate_schedule",
            {"schedule_id": "schedule-9"},
        ),
        (OPERATOR, "POST", "/nodes/node-9/drain", "drain_node", {"node_id": "node-9"}),
    ],
    ids=[
        "job",
        "hold",
        "release",
        "schedule",
        "delete",
        "operate a job",
        "operate a schedule",
        "drain a node",
    ],
)
def test_an_unknown_job_schedule_or_node_is_a_404_and_a_tool_error(  # noqa: PLR0913 - one call, both ways
    world: World,
    caller: str,
    method: str,
    path: str,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    seen: list[object] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        seen.append((await jobsvc.http(method, path, caller)).status_code)
        seen.append((await jobsvc.tool(caller, tool, arguments)).is_error)

    play(world, scenario)

    assert seen == [HTTPStatus.NOT_FOUND, True]


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("POST", "/schedules", "submit"),
        ("GET", "/schedules", "read"),
        ("GET", "/schedules/schedule-1", "read"),
        ("GET", "/nodes", "read"),
        ("GET", "/usage", "read"),
    ],
    ids=["create a schedule", "list schedules", "read a schedule", "list nodes", "usage"],
)
def test_a_caller_without_the_permission_a_route_needs_is_refused_and_told_how_to_ask(
    world: World,
    method: str,
    path: str,
    permission: str,
) -> None:
    replies: list[tuple[int, object]] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        refused = await jobsvc.http(method, path, OUTSIDER, SCHEDULE if method == "POST" else None)
        replies.append((refused.status_code, refused.json()["detail"]["permission"]))

    play(world, scenario)

    assert replies == [(HTTPStatus.FORBIDDEN, permission)]


@pytest.mark.parametrize(
    ("method", "path", "tool", "arguments"),
    [
        ("POST", "/operations/jobs/{job}/cancel", "operate_job", {"job_id": "{job}", "action": "cancel"}),
        ("DELETE", "/operations/schedules/{schedule}", "operate_schedule", {"schedule_id": "{schedule}"}),
        ("POST", "/nodes/node-1/drain", "drain_node", {"node_id": "node-1"}),
    ],
    ids=["operate a job", "operate a schedule", "drain a node"],
)
def test_an_operators_route_needs_operate_which_no_one_may_request_even_for_their_own(
    world: World,
    method: str,
    path: str,
    tool: str,
    arguments: dict[str, str],
) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        job = (await jobsvc.http("POST", "/jobs", OWNER, JOB)).json()
        schedule = (await jobsvc.http("POST", "/schedules", OWNER, SCHEDULE)).json()
        ids = {"job": job["id"], "schedule": schedule["id"]}
        refused = await jobsvc.http(method, path.format(**ids), OWNER)
        seen["http"] = (refused.status_code, refused.json()["detail"]["request_with"])
        own = {name: value.format(**ids) for name, value in arguments.items()}
        seen["tool"] = text(await jobsvc.tool(OWNER, tool, own))

    play(world, scenario)

    assert seen == {"http": (HTTPStatus.FORBIDDEN, None), "tool": "not permitted: operate on jobsvc."}


def test_an_operator_cancels_holds_and_releases_anyones_job_and_deletes_anyones_schedule(
    world: World,
) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        waiting = (await jobsvc.http("POST", "/jobs", OWNER, JOB)).json()
        held = await jobsvc.http("POST", f"/operations/jobs/{waiting['id']}/hold", OPERATOR)
        seen["held"] = (held.status_code, held.json()["state"])
        released = await jobsvc.tool(OPERATOR, "operate_job", {"job_id": waiting["id"], "action": "release"})
        seen["released"] = (released.structured_content or {})["state"]
        cancelled = await jobsvc.http("POST", f"/operations/jobs/{waiting['id']}/cancel", OPERATOR)
        seen["cancelled"] = (cancelled.status_code, cancelled.json()["state"])
        schedule = (await jobsvc.http("POST", "/schedules", OWNER, SCHEDULE)).json()
        deleted = await jobsvc.tool(OPERATOR, "operate_schedule", {"schedule_id": schedule["id"]})
        seen["deleted"] = (deleted.structured_content or {})["next_run_at"]

    play(world, scenario)

    assert seen == {
        "held": (HTTPStatus.OK, "held"),
        "released": "pending",
        "cancelled": (HTTPStatus.ACCEPTED, "cancelled"),
        "deleted": None,
    }


def test_a_drained_nodes_job_runs_again_elsewhere_under_its_job_id_and_credential_as_attempt_2(
    world: World,
) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await start_job(world, jobsvc)  # on node-1, its credential jobsvc.tokens["job"]
        drained = await jobsvc.http("POST", "/nodes/node-1/drain", OPERATOR)
        seen["drained"] = (drained.status_code, drained.json()["state"])
        world.clock.advance(GRACE)
        world.service.reconcile()  # the grace is over: its process is stopped
        world.service.reconcile()  # once it is gone, the job is queued again and starts on node-2
        moved = (
            await jobsvc.http("GET", f"/jobs/{submitted['id']}", "job")
        ).json()  # read with its credential
        seen["moved"] = (moved["node"], moved["attempt"], moved["principal"] == submitted["principal"])
        # The credential and the badge its first attempt was handed, and nothing else.
        seen["same credential"] = world.nodes[1].environments == world.nodes[0].environments[-1:]
        seen["same credential"] &= world.nodes[1].environments[0][JOB_SECRET] == SecretStr(
            jobsvc.tokens["job"],
        )
        nodes = (await jobsvc.tool(OTHER, "list_nodes", {})).structured_content or {}
        seen["nodes"] = [(node["id"], node["state"]) for node in nodes["nodes"]]

    play(world, scenario)

    assert seen == {
        "drained": (HTTPStatus.ACCEPTED, "draining"),
        "moved": ("node-2", 2, True),
        "same credential": True,
        "nodes": [("node-1", "drained"), ("node-2", "busy")],
    }


def test_a_schedules_runs_all_run_as_its_one_workload_and_stop_after_delete_schedule(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        schedule = (await jobsvc.tool(OWNER, "create_schedule", SCHEDULE)).structured_content or {}
        for _ in range(2):
            world.clock.advance(INTERVAL)
            world.service.reconcile()  # the last run ends, and the next starts
            world.node.exit(0)
        seen["deleted"] = (await jobsvc.http("DELETE", f"/schedules/{schedule['id']}", OWNER)).status_code
        world.clock.advance(INTERVAL)
        world.service.reconcile()  # the second run ends, and no third starts
        found = (await jobsvc.http("GET", f"/schedules/{schedule['id']}", OTHER)).json()
        runs = [(await jobsvc.http("GET", f"/jobs/{run}", OTHER)).json() for run in found["runs"]]
        seen["runs"] = [
            (run["schedule_id"], run["principal"] == schedule["principal"], run["state"]) for run in runs
        ]
        seen["credentials"] = len({started[JOB_SECRET] for started in world.node.environments})

    play(world, scenario)

    issued = [line for line in world.iam.audit_log() if line["kind"] == "workload_issued"]
    assert seen == {
        "deleted": HTTPStatus.OK,
        "runs": [("schedule-1", True, "succeeded")] * 2,
        "credentials": 1,
    }
    assert [(line["operation"], line["reference"], line["result"]["sponsor"]) for line in issued] == [
        ("create_schedule", "schedule-1", OWNER),
    ]


def test_usage_is_the_cpu_hours_jobs_ran_from_their_wall_time_by_team_and_by_job(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        await jobsvc.http("POST", "/jobs", OWNER, {**JOB, "cpu": 2})
        world.service.reconcile()
        world.clock.advance(1800)  # 2 cpus for half an hour
        seen["usage"] = (await jobsvc.tool(OTHER, "usage", {})).structured_content

    play(world, scenario)

    assert seen == {"usage": {"teams": {"research-platform": 1.0}, "jobs": {"job-1": 1.0}}}


def test_a_submission_the_run_configs_scope_refuses_is_a_403_and_queues_nothing(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        refused = await jobsvc.http("POST", "/jobs", UNSCOPED, JOB)
        seen["job"] = (refused.status_code, refused.json()["detail"])
        seen["schedule"] = (await jobsvc.http("POST", "/schedules", UNSCOPED, SCHEDULE)).status_code
        seen["by tool"] = text(await jobsvc.tool(UNSCOPED, "submit_job", JOB))

    play(world, scenario)

    assert seen == {
        "job": (HTTPStatus.FORBIDDEN, "submit_job requires submit_job"),
        "schedule": HTTPStatus.FORBIDDEN,
        "by tool": "Error executing tool submit_job: submit_job requires submit_job",
    }
    assert (world.service.list_jobs(), world.service.list_schedules()) == ([], [])
