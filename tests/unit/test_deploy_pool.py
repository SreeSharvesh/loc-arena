"""A stack run's deployed services: each under a supervisor that the deploy service alone can call.

Read from the compose file a stack run renders for the aurora run, whose deploy service deploys to the notes,
chat, tickets, eval, agents and job services and the model store.
"""

from __future__ import annotations

import pytest
from loc_arena.compose_document import ComposeDocument
from loc_arena.config import load_run_config
from loc_arena.deploy_pool import release_token_variable
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose

RUN = "aurora-efficiency"
AURORA = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
DEPLOYED = ("notes", "chat", "tickets", "modelstore", "evalkit", "agents", "jobsvc")
CONFIG_PATH = f"/app/configs/{RUN}.yaml"


@pytest.fixture
def compose() -> ComposeDocument:
    return render_compose(AURORA, REPOSITORY, RUN, [])


@pytest.mark.parametrize("service", DEPLOYED)
def test_a_deployed_service_runs_under_its_supervisor_holding_its_own_token_alone(
    compose: ComposeDocument,
    service: str,
) -> None:
    rendered = compose["services"][service]

    tokens = [secret for secret in rendered["secrets"] if "release_token" in str(secret)]

    assert (rendered["command"], tokens) == (
        ["python", "-m", "loc_arena.release_supervisor", CONFIG_PATH, service],
        [{"source": f"release_token_{service}", "target": "release_token"}],
    )


def test_the_deploy_service_holds_every_supervisors_token_and_runs_under_none(
    compose: ComposeDocument,
) -> None:
    rendered = compose["services"]["deploy"]

    tokens = [secret for secret in rendered["secrets"] if "release_token" in str(secret)]

    assert (rendered["command"], tokens) == (
        ["python", "-m", "loc_arena.deploy_service", CONFIG_PATH, "deploy"],
        [f"release_token_{service}" for service in DEPLOYED],
    )


def test_a_service_the_deploy_service_does_not_deploy_to_runs_its_own_module_with_no_token(
    compose: ComposeDocument,
) -> None:
    rendered = compose["services"]["forge"]

    tokens = [secret for secret in rendered["secrets"] if "release_token" in str(secret)]

    assert (rendered["command"], tokens) == (
        ["python", "-m", "loc_arena.forge_service", CONFIG_PATH, "forge"],
        [],
    )


def test_each_play_gets_each_supervisor_a_token_of_its_own() -> None:
    attack, twin = issue_secrets(AURORA)

    issued = [play[release_token_variable(service)] for play in (attack, twin) for service in DEPLOYED]

    assert len(set(issued)) == 2 * len(DEPLOYED)
