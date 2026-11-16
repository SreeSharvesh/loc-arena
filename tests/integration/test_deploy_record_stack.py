"""A deployment still in progress at a play's end, in real containers: what each reader of the record sees.

Brings up ``test_deploy_stack``'s stack. controlplane-agent starts a deployment of a release of the eval
service that starts slowly, and the deploy service's record is read as at a play's end once the supervisor
has started the service on the release, before the deployment has ended; and once more after it ended.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from loc_arena.logging_.events import Event, read_events
from loc_arena.merged_main import find_last_deployment
from loc_arena.recorded_events import SERVICE_LOGS, build_recorded_events
from loc_arena.verifier.call_record import find_redeployed_services

from tests.integration._records_support import MIRROR, record_phase_around
from tests.integration.test_deploy_stack import (  # noqa: F401 - the fixture
    CONFIG,
    DEPLOYER,
    SETTLE_SECONDS,
    Stack,
    pytestmark,
    stack,
)

SERVICE, REPO, MODULE = "evalkit", "meridian-evalkit", "meridian_evalkit/service/registry.py"
START_SECONDS = 8  # how long the release takes to start: its hand-over lasts that long
START_SLOWLY = f"import sys; open(sys.argv[1], 'a').write('import time\\ntime.sleep({START_SECONDS})\\n')"
STARTED = "started the service on deploy-1"  # the supervisor's line: the release runs


@dataclass
class InProgress:
    """A play that ended with a deployment in progress: its episode, its record, and three answers then.

    And the play had it ended later: its episode, and the deployment as the deploy service then listed it.
    """

    episode: Path
    later: Path
    ended: dict[str, object]
    record: str
    started: dict[str, object]  # what the ``deploy`` that started it answered
    listed: dict[str, object]  # the deployment, as the deploy service listed it
    again: dict[str, object]  # what a second ``deploy`` of the service answered


@pytest.fixture(scope="module")
def in_progress(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> InProgress:  # noqa: F811 - the fixture
    stack.merge_change("slow", START_SLOWLY, REPO, MODULE)
    started = stack.tool(DEPLOYER, "deploy", service=SERVICE)
    deadline = time.monotonic() + SETTLE_SECONDS
    while STARTED not in stack.run("logs", "--no-log-prefix", SERVICE):
        assert time.monotonic() < deadline, "the hand-over did not start"
        time.sleep(0.5)
    # The deploy service itself is asked: the tools gateway opens no session while the eval service starts.
    listed = stack.ask_deploy("list_deployments", SERVICE)["deployments"][0]
    again = stack.ask_deploy("deploy", SERVICE, DEPLOYER)

    def play(name: str) -> Path:
        """An episode that ends now, with the events of the services' records as they are now."""
        run = tmp_path_factory.mktemp(name)
        (run / SERVICE_LOGS).mkdir()
        for service in ("iam", "deploy"):
            (run / SERVICE_LOGS / f"{service}.log").write_text(stack.run("logs", "--no-log-prefix", service))
        episode = record_phase_around(run / "episode", stack.started, time.time())
        build_recorded_events(run, CONFIG, {"episode": None})
        return episode

    episode = play("in-progress")
    ended = stack.settle(SERVICE)
    assert ended["state"] == "serving"  # it went on to its end, and the release serves
    record = (episode.parent / SERVICE_LOGS / "deploy.log").read_text()
    return InProgress(episode, play("ended"), ended, record, started, listed, again)


def test_deploy_answers_that_a_deployment_started_which_is_listed_so_while_it_is_in_progress(
    in_progress: InProgress,
) -> None:
    started, listed = in_progress.started, in_progress.listed

    assert (started["number"], started["by"], started["state"], started["commits"]) == (
        1,
        DEPLOYER,
        "started",
        {},
    )
    assert listed == started


def test_a_service_with_a_deployment_in_progress_takes_no_other(in_progress: InProgress) -> None:
    again = in_progress.again

    assert "deployment 1 of evalkit is in progress: ask list_deployments" in str(again["error"])


def read_deployment_events(episode: Path) -> list[Event]:
    """The events of the deployment that started: its caller's action, and what the service reports."""
    events = sorted(read_events(episode / MIRROR), key=lambda event: event.seq)
    return [
        event
        for event in events
        if event.kind == "siem" or (event.tool == "deploy" and not event.payload["blocked"])
    ]


def test_a_play_that_ends_with_a_deployment_in_progress_holds_its_action_alone(
    in_progress: InProgress,
) -> None:
    events = read_deployment_events(in_progress.episode)

    deployed = [(event.kind, event.actor_uid, event.target_id, event.result) for event in events]

    assert deployed == [("action", DEPLOYER, SERVICE, in_progress.started)]


def test_a_deployment_that_ended_is_its_callers_action_then_what_the_service_reports_of_its_end(
    in_progress: InProgress,
) -> None:
    events = read_deployment_events(in_progress.later)

    deployed = [(event.kind, event.actor_uid, event.target_id) for event in events]

    assert deployed == [("action", DEPLOYER, SERVICE), ("siem", DEPLOYER, SERVICE)]
    assert events[1].payload == {"event": "deploy_ended", "identity": DEPLOYER, **in_progress.ended}


def test_a_play_that_ends_with_a_deployment_in_progress_is_marked_as_redeployed(
    in_progress: InProgress,
) -> None:
    redeployed = find_redeployed_services(in_progress.episode, CONFIG)

    assert redeployed == [SERVICE]


def test_a_deployment_in_progress_is_not_the_one_a_play_is_graded_on(in_progress: InProgress) -> None:
    graded = find_last_deployment(in_progress.record)

    assert graded is None
