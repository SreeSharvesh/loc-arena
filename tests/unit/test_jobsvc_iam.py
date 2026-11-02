"""The job service under central IAM: a real IAM server in front of its HTTP API and its tools, a fake fleet.

The app is the one a stack run serves (``tests.unit._jobsvc_support``), whose node keeps the environment each
job was started with.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import jwt
import pytest
from loc_arena.jobsvc_service import IamWorkloadIssuer, serving_app
from loc_arena.tool_records import ToolRecord

from meridian_iam.contracts import ManifestError
from meridian_jobsvc.errors import CredentialRefused
from meridian_jobsvc.fleet import ProcessState
from tests.unit._iam_waits import wait_until_refused
from tests.unit._jobsvc_support import (
    ACCOUNT,
    ASK,
    JOB,
    MANIFEST,
    OTHER,
    OUTSIDER,
    OWNER,
    Jobsvc,
    World,
    play,
    serve_world,
    start_job,
    text,
)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    yield from serve_world(tmp_path)


def test_a_job_submitted_over_http_runs_on_a_node_and_is_cancelling_until_its_process_is_gone(
    world: World,
) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await jobsvc.http("POST", "/jobs", OWNER, JOB)
        job_id = submitted.json()["id"]
        seen["submitted"] = (submitted.status_code, submitted.json()["owner"], submitted.json()["state"])
        world.service.reconcile()
        world.node.stdout = "step 1\n"
        read = (await jobsvc.http("GET", f"/jobs/{job_id}", OTHER)).json()
        seen["running"] = (read["state"], read["node"])
        seen["logs"] = (await jobsvc.http("GET", f"/jobs/{job_id}/logs", OTHER)).json()["stdout"]
        cancelled = await jobsvc.http("DELETE", f"/jobs/{job_id}", OWNER)
        seen["cancelling"] = (cancelled.status_code, cancelled.json()["state"], world.node.state)
        world.service.reconcile()  # stops the process
        world.service.reconcile()  # sees it gone
        seen["ended"] = (await jobsvc.http("GET", "/jobs", OTHER)).json()["jobs"][0]["state"]

    play(world, scenario)

    assert seen == {
        "submitted": (HTTPStatus.CREATED, OWNER, "pending"),
        "running": ("running", "node-1"),
        "logs": "step 1\n",
        "cancelling": (HTTPStatus.ACCEPTED, "cancelling", ProcessState.RUNNING),
        "ended": "cancelled",
    }


def test_a_started_job_calls_the_model_gateway_with_its_own_badge_naming_its_submitter(world: World) -> None:
    seen: dict[str, Any] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        seen["job"] = await start_job(world, jobsvc)

    play(world, scenario)

    badge = world.node.environments[-1]["MERIDIAN_GATEWAY_TOKEN"].get_secret_value()
    public = jwt.PyJWKSet.from_dict(dict(world.iam.key_set())).keys[0].key
    claims = jwt.decode(
        badge,
        public,
        algorithms=["ES256"],
        audience="agentgateway",
        options={"verify_exp": False},
    )
    assert (claims["sub"], claims["sponsor"]) == (seen["job"]["principal"], OWNER)


def test_a_job_submitted_through_the_tools_is_its_callers_and_no_other_agent_cancels_it(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await jobsvc.tool(OWNER, "submit_job", {**JOB, "labels": {"team": "serving"}})
        job_id = str((submitted.structured_content or {})["id"])
        world.service.reconcile()
        refused = await jobsvc.tool(OTHER, "cancel_job", {"job_id": job_id})
        seen["over mcp"] = (refused.is_error, text(refused))
        seen["over http"] = (await jobsvc.http("DELETE", f"/jobs/{job_id}", OTHER)).status_code
        read = await jobsvc.tool(OTHER, "get_job", {"job_id": job_id})
        seen["job"] = {key: (read.structured_content or {})[key] for key in ("owner", "state", "labels")}

    play(world, scenario)

    assert seen == {
        "over mcp": (True, f"Error executing tool cancel_job: job 'job-1' is {OWNER}'s, not yours"),
        "over http": HTTPStatus.FORBIDDEN,
        "job": {"owner": OWNER, "state": "running", "labels": {"team": "serving"}},
    }


def test_a_call_without_a_credential_or_the_permission_runs_nothing(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        seen["no credential"] = (await jobsvc.http("POST", "/jobs", None, JOB)).status_code
        denied = await jobsvc.http("POST", "/jobs", OUTSIDER, JOB)
        seen["no right"] = (denied.status_code, denied.json()["detail"]["permission"])
        refused = await jobsvc.tool(OUTSIDER, "submit_job", JOB)
        seen["no right, by tool"] = text(refused)
        seen["reads"] = (await jobsvc.http("GET", "/jobs", OUTSIDER)).status_code

    play(world, scenario)
    world.service.reconcile()

    assert seen == {
        "no credential": HTTPStatus.UNAUTHORIZED,
        "no right": (HTTPStatus.FORBIDDEN, "submit"),
        "no right, by tool": f"not permitted: submit on jobsvc. Ask for it with: {ASK}.",
        "reads": HTTPStatus.FORBIDDEN,
    }
    assert (world.service.list_jobs(), world.node.started) == ([], [])


def test_a_body_naming_who_owns_the_job_is_refused(world: World) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        replies.append((await jobsvc.http("POST", "/jobs", OUTSIDER, {**JOB, "owner": OWNER})).status_code)
        replies.append((await jobsvc.http("POST", "/jobs", OTHER, {**JOB, "owner": OWNER})).status_code)

    play(world, scenario)

    # The authority decides before the body is read; a caller it allows still names no owner but itself.
    assert (replies, world.service.list_jobs()) == (
        [HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY],
        [],
    )


def test_the_description_health_and_schema_need_no_credential(world: World) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        for path in ("/", "/health", "/openapi.json"):
            replies.append((await jobsvc.http("GET", path, None)).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.OK] * 3


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            lambda manifest: {
                **manifest,
                "tools": {k: v for k, v in manifest["tools"].items() if k != "job_logs"},
            },
            "undeclared tool job_logs",
        ),
        (
            lambda manifest: {**manifest, "http": {**manifest["http"], "GET /queues": "list_jobs"}},
            "declared but unregistered route GET /queues",
        ),
    ],
    ids=["a tool it serves is not declared", "a declared route is not served"],
)
def test_the_service_starts_only_if_its_routes_and_tools_are_exactly_its_manifests(
    world: World,
    change: Callable[[dict[str, Any]], dict[str, Any]],
    problem: str,
) -> None:
    guard = world.guard(change(MANIFEST))

    with pytest.raises(ManifestError, match=problem):
        serving_app(world.service, guard, world.records.append)


def test_an_iam_outage_is_a_503_and_runs_nothing(world: World) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        world.iam_server.should_exit = True
        wait_until_refused(world.iam_url)
        replies.append((await jobsvc.http("POST", "/jobs", OWNER, JOB)).status_code)

    play(world, scenario)

    assert (replies, world.service.list_jobs()) == ([HTTPStatus.SERVICE_UNAVAILABLE], [])


def test_each_request_tool_call_and_change_of_a_jobs_state_is_one_line_of_the_record(world: World) -> None:
    async def scenario(jobsvc: Jobsvc) -> None:
        await jobsvc.http("POST", "/jobs", OWNER, JOB)
        world.service.reconcile()
        await jobsvc.tool(OTHER, "get_job", {"job_id": "job-1"})

    play(world, scenario)

    lines = [json.loads(line) for line in world.records]
    requests = [line for line in lines if line.get("operation") == "submit_job"]
    states = [line["job"]["state"] for line in lines if "job" in line]
    calls = [ToolRecord.model_validate(line) for line in lines if "gen_ai.tool.name" in line]
    assert [(line["caller"], line["status"], bool(line["decision"])) for line in requests] == [
        (OWNER, HTTPStatus.CREATED, True),
    ]
    assert (states, [(call.tool, call.agent, bool(call.iam_decision)) for call in calls]) == (
        ["pending", "running"],
        [("get_job", OTHER, True)],
    )


def test_a_job_calls_the_service_as_its_own_principal_allowed_exactly_what_iam_gave_it(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await start_job(world, jobsvc)
        read = await jobsvc.http("GET", f"/jobs/{submitted['id']}", "job")
        seen["read"] = (read.status_code, read.json()["principal"] == submitted["principal"] != OWNER)
        refused = await jobsvc.http("POST", "/jobs", "job", JOB)  # its owner may submit; a job may not
        seen["submit"] = (refused.status_code, refused.json()["detail"]["permission"])
        seen["recorded as"] = json.loads(world.records[-1])["caller"] == submitted["principal"]

    play(world, scenario)

    assert seen == {
        "read": (HTTPStatus.OK, True),
        "submit": (HTTPStatus.FORBIDDEN, "submit"),
        "recorded as": True,
    }


def test_a_job_holds_what_its_owner_holds_of_what_a_job_may_be_given_and_no_more(world: World) -> None:
    world.iam.allow_delegation("jobsvc", {"jobsvc": ["submit"]})  # now a job may read and submit
    world.iam.revoke(world.tokens["admin"], OTHER, "jobsvc", ["read"], "r")  # OTHER may submit, not read
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        await start_job(world, jobsvc, OTHER)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)
        replies.append((await jobsvc.http("POST", "/jobs", "job", JOB)).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.FORBIDDEN, HTTPStatus.CREATED]


@pytest.mark.parametrize(
    ("lose", "refused"),
    [
        (
            lambda world: world.iam.revoke(world.tokens["admin"], OWNER, "jobsvc", ["read"], "r"),
            HTTPStatus.FORBIDDEN,
        ),
        (lambda world: world.iam.disable_principal(OWNER), HTTPStatus.UNAUTHORIZED),
    ],
    ids=["its owner's grant revoked", "its owner disabled"],
)
def test_a_jobs_next_call_is_refused_once_its_owner_loses_the_grant_it_copied(
    world: World,
    lose: Callable[[World], object],
    refused: HTTPStatus,
) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        await start_job(world, jobsvc)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)
        lose(world)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.OK, refused]


def test_a_job_submitted_with_the_service_accounts_credential_outlives_the_agent_that_submitted_it(
    world: World,
) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        # The agent submits by presenting the durable service account's credential, so its job is sponsored by
        # the account, not by the agent's own identity.
        await start_job(world, jobsvc, ACCOUNT)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)
        world.iam.disable_principal(OWNER)  # the agent that submitted it is stopped: the job carries on
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)
        world.iam.disable_principal(ACCOUNT)  # only stopping the account itself stops its work
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.OK, HTTPStatus.OK, HTTPStatus.UNAUTHORIZED]


def test_a_job_id_issued_once_is_refused_another_workload_so_none_passes_to_another_caller(
    world: World,
) -> None:
    issuer = IamWorkloadIssuer(world.client(), 7200, {"jobsvc": 3600})  # as a restarted service would
    issuer.issue(world.tokens[OWNER], "submit_job", "job-1", "bench")

    with pytest.raises(CredentialRefused, match="idempotency key reused"):
        issuer.issue(world.tokens[OTHER], "submit_job", "job-1", "bench")
