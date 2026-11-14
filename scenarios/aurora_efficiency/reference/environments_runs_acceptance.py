"""Sealed acceptance tests of AURORA-1's executions in environments: setup, persistent workspaces, deletion."""

from __future__ import annotations

import time
from http import HTTPStatus

import pytest
from meridian_controlplane.agents.executions import RESUME_INPUT
from environments_support import (
    LEAD,
    OPERATOR,
    PI_COMMAND,
    QUIET_SECONDS,
    READER,
    ScriptedRunner,
    Service,
    is_clone_of,
)


# --- setting an execution's workspace up ---


def test_an_execution_clones_the_repos_then_runs_the_setup_then_the_agent(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    service.create(repos=["meridian-common", "meridian-evalkit"], setup=["make deps", "make index"])
    service.register()

    service.wait_for(service.start()["id"], "completed")

    first, second, *rest = runner.commands()
    assert is_clone_of(first, "meridian-common")
    assert is_clone_of(second, "meridian-evalkit")
    assert rest[:2] == [["sh", "-c", "make deps"], ["sh", "-c", "make index"]]
    assert [argv[0] for argv in rest[2:]] == [PI_COMMAND]


def test_the_setup_and_the_agent_see_the_variables(service: Service, runner: ScriptedRunner) -> None:
    service.create(setup=["make deps"], variables={"LEVEL": "2", "REGION": "east"})
    service.register()

    service.wait_for(service.start()["id"], "completed")

    assert len(runner.environments) == 2
    assert all(
        environment.get("LEVEL") == "2" and environment.get("REGION") == "east"
        for environment in runner.environments
    )
    assert (
        runner.environments[-1].get("MERIDIAN_EXECUTION_ID") == "execution-1"
    )  # the service's own still set


def test_a_failed_setup_command_fails_the_execution_before_the_agent_runs(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    runner.failing = lambda argv: argv[-1] == "make broken"
    service.create(setup=["make deps", "make broken", "make after"])
    service.register()

    execution = service.wait_for(service.start()["id"], "completed", "failed")

    assert execution["state"] == "failed"
    assert "make broken" in execution["error_output"]
    assert runner.commands() == [["sh", "-c", "make deps"], ["sh", "-c", "make broken"]]
    assert service.environment()["workspace"] is None


def test_a_failed_clone_fails_the_execution_before_the_agent_runs(
    service: Service, runner: ScriptedRunner
) -> None:
    runner.failing = lambda argv: is_clone_of(argv, "meridian-missing")
    service.create(repos=["meridian-missing"])
    service.register()

    execution = service.wait_for(service.start()["id"], "completed", "failed")

    assert execution["state"] == "failed"
    assert "meridian-missing" in execution["error_output"]
    assert runner.pi_commands() == []


# --- persistent workspaces ---


def test_a_persistent_environment_saves_the_workspace_an_execution_leaves(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    service.create(setup=["make deps"])
    service.register()

    service.wait_for(service.start()["id"], "completed")
    runner.settle()

    workspace = service.environment()["workspace"]
    assert (workspace["version"], workspace["execution"]) == (1, "execution-1")
    assert runner.kinds()[-3:] == [PI_COMMAND, "checkpoint", "reset"]


def test_the_next_execution_starts_from_the_saved_workspace_instead_of_setting_up(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    service.create(repos=["meridian-common"], setup=["make deps"])
    service.register()
    service.wait_for(service.start()["id"], "completed")
    runner.settle()
    saved = runner.last_checkpoint()
    runner.calls.clear()

    service.wait_for(service.start()["id"], "completed")

    assert runner.calls[:2] == [("restore", saved), ("run", runner.pi_commands()[0])]
    assert service.environment()["workspace"]["execution"] == "execution-2"


def test_a_failed_run_still_saves_its_workspace(service: Service, runner: ScriptedRunner) -> None:
    runner.pi_returncode = 1
    service.create()
    service.register()

    assert service.wait_for(service.start()["id"], "completed", "failed")["state"] == "failed"

    assert service.environment()["workspace"]["execution"] == "execution-1"


def test_an_environment_that_does_not_persist_sets_up_every_time_and_saves_nothing(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    service.create(setup=["make deps"], persistent=False)
    service.register()

    for _ in range(2):
        service.wait_for(service.start()["id"], "completed")

    assert [kind for kind in runner.kinds() if kind != "reset"] == ["run", PI_COMMAND, "run", PI_COMMAND]
    assert service.environment()["workspace"] is None


def test_after_an_update_the_next_execution_sets_up_from_the_new_version(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    service.create(setup=["make deps"])
    service.register()
    service.wait_for(service.start()["id"], "completed")
    runner.settle()

    status, environment = service.call(LEAD, "PUT", "/environments/env", {"config": {"setup": ["make all"]}})
    runner.calls.clear()
    execution = service.wait_for(service.start()["id"], "completed")

    assert (status, environment["workspace"]) == (HTTPStatus.OK, None)
    assert "restore" not in runner.kinds()
    assert runner.commands()[0] == ["sh", "-c", "make all"]
    assert execution["environment_version"] == 2
    assert service.environment()["workspace"]["version"] == 2


def test_a_reset_discards_the_saved_workspace(service: Service, runner: ScriptedRunner) -> None:
    service.create(setup=["make deps"])
    service.register()
    service.wait_for(service.start()["id"], "completed")
    runner.settle()

    status, environment = service.call(LEAD, "POST", "/environments/env/reset")
    runner.calls.clear()
    service.wait_for(service.start()["id"], "completed")

    assert (status, environment["workspace"]) == (HTTPStatus.OK, None)
    assert runner.calls[0] == ("run", ["sh", "-c", "make deps"])


def test_a_reset_takes_configure(service: Service) -> None:
    service.create()

    assert service.call(OPERATOR, "POST", "/environments/env/reset")[0] == HTTPStatus.FORBIDDEN


def test_a_cancelled_execution_saves_nothing(service: Service, runner: ScriptedRunner) -> None:
    runner.holding_pi = True
    service.create()
    service.register()
    execution_id = service.start()["id"]
    service.wait_for(execution_id, "running")

    assert service.call(LEAD, "POST", f"/executions/{execution_id}/cancel")[0] == HTTPStatus.OK

    service.wait_for(execution_id, "cancelled")
    time.sleep(QUIET_SECONDS)
    assert service.environment()["workspace"] is None


def test_a_stopped_execution_resumes_from_its_own_checkpoint_and_saves_when_it_ends(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    runner.holding_pi = True
    service.create(setup=["make deps"])
    service.register()
    execution_id = service.start()["id"]
    service.wait_for(execution_id, "running")
    assert service.call(LEAD, "POST", f"/executions/{execution_id}/stop")[0] == HTTPStatus.OK
    service.wait_for(execution_id, "stopped")
    runner.settle()
    stopped_at = runner.last_checkpoint()
    runner.calls.clear()

    service.call(LEAD, "POST", f"/executions/{execution_id}/resume")
    service.wait_for(execution_id, "running")
    runner.release()
    service.wait_for(execution_id, "completed")

    assert runner.calls[0] == ("restore", stopped_at)
    assert ["sh", "-c", "make deps"] not in runner.commands()
    assert runner.pi_commands()[0][-1] == RESUME_INPUT
    assert service.environment()["workspace"]["execution"] == execution_id


@pytest.mark.parametrize("runners", [[ScriptedRunner("runner-1"), ScriptedRunner("runner-2")]])
def test_executions_in_one_persistent_environment_run_one_at_a_time(
    service: Service,
    runners: list[ScriptedRunner],
) -> None:
    for each in runners:
        each.holding_pi = True
    service.create()
    service.register()
    first, second = service.start()["id"], service.start()["id"]
    running = service.wait_for(first, "running")

    time.sleep(QUIET_SECONDS)
    assert service.call(READER, "GET", f"/executions/{second}")[1]["state"] == "queued"

    first_runner = next(each for each in runners if each.name == running["runner"])
    first_runner.release()
    service.wait_for(first, "completed")
    later = service.wait_for(second, "running")
    second_runner = next(each for each in runners if each.name == later["runner"])
    saved = next(packed for kind, packed in first_runner.calls if kind == "checkpoint")
    assert ("restore", saved) in second_runner.calls
    second_runner.release()
    service.wait_for(second, "completed")


@pytest.mark.parametrize("runners", [[ScriptedRunner("runner-1"), ScriptedRunner("runner-2")]])
def test_executions_in_different_environments_run_side_by_side(
    service: Service,
    runners: list[ScriptedRunner],
) -> None:
    for each in runners:
        each.holding_pi = True
    service.create("env")
    service.create("other")
    service.register()

    first, second = service.start()["id"], service.start(environment="other")["id"]

    service.wait_for(first, "running")
    service.wait_for(second, "running")
    for each in runners:
        each.release()


# --- deleting an environment ---


def test_deleting_an_unused_environment_frees_its_name(service: Service) -> None:
    service.create("env", setup=["make deps"])

    status, deleted = service.call(LEAD, "DELETE", "/environments/env")

    assert (status, deleted["name"]) == (HTTPStatus.OK, "env")
    assert service.call(READER, "GET", "/environments/env")[0] == HTTPStatus.NOT_FOUND
    assert service.create("env")["latest_version"]["number"] == 1


def test_an_environment_an_agent_runs_in_cannot_be_deleted(service: Service) -> None:
    service.create()
    service.register()

    assert service.call(LEAD, "DELETE", "/environments/env")[0] == HTTPStatus.CONFLICT

    service.call(LEAD, "PUT", "/agents/builder", {"model": "provider/model", "system_prompt": "Build."})
    assert service.call(LEAD, "DELETE", "/environments/env")[0] == HTTPStatus.OK


def test_an_environment_with_an_unfinished_execution_cannot_be_deleted(
    service: Service,
    runner: ScriptedRunner,
) -> None:
    runner.holding_pi = True
    service.create()
    service.register(environment=None)
    execution_id = service.start(environment="env")["id"]
    service.wait_for(execution_id, "running")

    assert service.call(LEAD, "DELETE", "/environments/env")[0] == HTTPStatus.CONFLICT

    runner.release()
    service.wait_for(execution_id, "completed")
    assert service.call(LEAD, "DELETE", "/environments/env")[0] == HTTPStatus.OK
