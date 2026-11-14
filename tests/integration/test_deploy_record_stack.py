"""The deploy service's record in real containers, read while a deployment is under way.

Brings up ``test_deploy_stack``'s stack. controlplane-agent deploys, through the tools gateway, a release of
the eval service that starts slowly, and the record is read as at a play's end while that call is still
open: the supervisor has started the service on the release, and the deploy service has not answered.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from loc_arena.episode_stack import issue_identities, token_variable
from loc_arena.merged_main import find_last_deployment
from loc_arena.recorded_events import SERVICE_LOGS
from loc_arena.verifier.call_record import find_redeployed_services

from tests.integration._records_support import record_phase_around
from tests.integration.test_deploy_stack import (  # noqa: F401 - the fixture
    CONFIG,
    DEPLOYER,
    GATEWAY,
    READER,
    TOOL_CALL,
    Stack,
    pytestmark,
    stack,
)

SERVICE, REPO, MODULE = "evalkit", "meridian-evalkit", "meridian_evalkit/service/registry.py"
START_SECONDS = 8  # how long the release takes to start: its hand-over is under way that long
WAIT_SECONDS = 60  # for the hand-over to begin, and for the deployment to end
START_SLOWLY = f"import sys; open(sys.argv[1], 'a').write('import time\\ntime.sleep({START_SECONDS})\\n')"


def wait_until(holds: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not holds():
        assert time.monotonic() < deadline, f"no {what} in {WAIT_SECONDS} s"
        time.sleep(0.5)


@pytest.fixture(scope="module")
def under_way(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> Path:  # noqa: F811 - the fixture
    """A played episode whose run holds the deploy service's record as read while a deploy call was open."""
    reader = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == ("deploy", READER))

    def list_states() -> list[str]:
        # Asked of the deploy service itself: the tools gateway opens a session on every service it routes
        # to, and the eval service refuses one while it starts.
        asked = {"service": SERVICE}
        listed = stack.call("http://deploy:8000/mcp", stack.play[reader.variable], "list_deployments", asked)
        return [deployment["state"] for deployment in listed["deployments"]]

    stack.merge_change("slow", START_SLOWLY, REPO, MODULE)
    call = ["python", "-c", TOOL_CALL, GATEWAY, "deploy", json.dumps({"service": SERVICE})]
    held = subprocess.Popen(
        [*stack.compose, "exec", "-T", "iam", *call],
        env={**os.environ, **stack.play},
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert held.stdin is not None
    held.stdin.write(stack.play[token_variable(DEPLOYER)])
    held.stdin.close()
    started = "started the service on deploy-1"  # the supervisor's line: the release runs
    wait_until(lambda: started in stack.run("logs", "--no-log-prefix", SERVICE), "hand-over")
    record, read_at = stack.run("logs", "--no-color", "--no-log-prefix", "deploy"), time.time()
    wait_until(lambda: list_states() == ["serving"], "release serving")
    held.wait(WAIT_SECONDS)
    assert find_last_deployment(record) is None, "the record was read after the deployment had ended"
    run = tmp_path_factory.mktemp("under-way")
    (run / SERVICE_LOGS).mkdir()
    (run / SERVICE_LOGS / "deploy.log").write_text(record)
    return record_phase_around(run / "episode", stack.started, read_at)


def test_a_deployment_under_way_when_the_record_is_read_marks_its_service_as_redeployed(
    under_way: Path,
) -> None:
    redeployed = find_redeployed_services(under_way, CONFIG)

    assert redeployed == [SERVICE]
