"""An eval run's job in real containers: the runner binds the run as itself, with what its node gave it.

Brings up the job service's stack (``test_jobsvc_stack``'s fixture) and the eval service and the forge beside
it. eval-agent starts a run with its identity on the eval service and submits the job the service handed out
with its identity on the cluster, as it would; the job, a real process on a node, imports the runner from the
company repos' main its node cloned from the forge, and binds the run under the workload IAM issued it, with
the forge's and the eval service's credentials the job service's delegation gives it. No model gateway runs,
so the runner's first model call fails and the job with it: the run stays started, bound to that job alone.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

from loc_arena.episode_stack import issue_identities

from tests.integration.test_jobsvc_stack import (  # noqa: F401 - the fixture
    CONFIG,
    SANDBOX,
    Stack,
    pytestmark,
    stack,
)

AGENT = "eval-agent"  # holds run on the eval service and submit on the cluster: it starts and submits a run
START = {"eval_id": "arithmetic", "model_id": "aurora-7b-v3"}

# Run in a container with python: one call of the eval service's API, as the holder of the token on stdin.
EVALKIT_CALL = """
import json, sys, urllib.error, urllib.request
method, path, body = sys.argv[1:4]
token = sys.stdin.read()
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
request = urllib.request.Request(f"http://evalkit:8000{path}", body.encode() or None, headers, method=method)
try:
    with urllib.request.urlopen(request, timeout=30) as reply:
        print(json.dumps([reply.status, json.loads(reply.read())]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, error.read().decode()]))
"""


def call_evalkit(running: Stack, agent: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
    """``agent``'s call of the eval service from serving-agent's sandbox, with ``agent``'s identity there."""
    found = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == ("evalkit", agent))
    payload = "" if body is None else json.dumps(body)
    output = running.run(
        "exec",
        "-T",
        SANDBOX,
        "python",
        "-c",
        EVALKIT_CALL,
        method,
        path,
        payload,
        standard_input=running.play[found.variable],
    )
    status, reply = json.loads(output.strip().splitlines()[-1])
    return status, reply


def test_an_eval_runs_job_binds_the_run_under_its_own_workload_and_fails_without_a_model(
    stack: Stack,  # noqa: F811 - the imported fixture
) -> None:
    stack.run("up", "--detach", "--wait", "--no-build", "evalkit", "forge")
    status, run = call_evalkit(stack, AGENT, "POST", "/runs", START)
    assert status == HTTPStatus.CREATED, run
    status, submitted = stack.http(AGENT, "POST", "/jobs", run["job"])
    assert status == HTTPStatus.CREATED, submitted

    job = stack.wait_for(submitted["id"], "succeeded", "failed")
    _, bound = call_evalkit(stack, AGENT, "GET", f"/runs/{run['run_id']}")
    logs = stack.http(AGENT, "GET", f"/jobs/{job['id']}/logs")[1]

    assert (job["state"], bound["state"], bound["job_principal"]) == (
        "failed",
        "started",
        job["principal"],
    ), logs
    assert "chat/completions" in logs["stderr"]  # the model call, the first thing it could not do
