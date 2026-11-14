"""Sealed acceptance tests of AURORA-1: environments are the service's records, so a service started again on
its database, as each deployment starts it, holds them, and a persistent environment's saved workspace with
them."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

from environments_support import LEAD, READER, ScriptedRunner, start_service
from fastapi import FastAPI

from meridian_controlplane.agents.executions import PI_COMMAND

Serve = Callable[[FastAPI], str]


def test_a_service_started_again_holds_each_environment_with_its_versions(serve: Serve, tmp_path: Path) -> None:
    first = start_service(serve, tmp_path / "agents.db", ScriptedRunner())
    first.create("app", repos=["meridian-common"])
    assert first.call(LEAD, "PUT", "/environments/app", {"config": {"setup": ["make build"]}})[0] == HTTPStatus.OK
    first.create("old")
    assert first.call(LEAD, "POST", "/environments/old/archive")[0] == HTTPStatus.OK
    listed = first.call(READER, "GET", "/environments")

    again = start_service(serve, tmp_path / "agents.db", ScriptedRunner())

    assert again.call(READER, "GET", "/environments") == listed
    status, versions = again.call(READER, "GET", "/environments/app/versions")
    assert status == HTTPStatus.OK
    assert [(version["number"], version["config"]["repos"], version["config"]["setup"]) for version in versions] == [
        (1, ["meridian-common"], []),
        (2, [], ["make build"]),
    ]


def test_an_environment_deleted_before_the_service_started_again_stays_deleted(serve: Serve, tmp_path: Path) -> None:
    first = start_service(serve, tmp_path / "agents.db", ScriptedRunner())
    first.create("gone")
    assert first.call(LEAD, "DELETE", "/environments/gone")[0] == HTTPStatus.OK

    again = start_service(serve, tmp_path / "agents.db", ScriptedRunner())

    assert again.call(READER, "GET", "/environments/gone")[0] == HTTPStatus.NOT_FOUND
    assert again.call(LEAD, "POST", "/environments", {"name": "gone"})[0] == HTTPStatus.CREATED


def test_an_execution_keeps_its_environment_when_the_service_starts_again(serve: Serve, tmp_path: Path) -> None:
    first = start_service(serve, tmp_path / "agents.db", ScriptedRunner())
    first.create()
    first.register()
    execution = first.start()
    first.wait_for(execution["id"], "completed")

    again = start_service(serve, tmp_path / "agents.db", ScriptedRunner())

    status, read = again.call(READER, "GET", f"/executions/{execution['id']}")
    assert status == HTTPStatus.OK
    assert (read["environment"], read["environment_version"]) == ("env", 1)


def test_a_service_started_again_restores_the_workspace_saved_before(serve: Serve, tmp_path: Path) -> None:
    first_runner = ScriptedRunner()
    first = start_service(serve, tmp_path / "agents.db", first_runner)
    first.create(repos=["meridian-common"], setup=["make build"])
    first.register()
    saving = first.start()
    first.wait_for(saving["id"], "completed")
    first_runner.settle()
    saved = first_runner.last_checkpoint()
    runner = ScriptedRunner(name="runner-2")

    again = start_service(serve, tmp_path / "agents.db", runner)

    assert again.environment()["workspace"]["execution"] == saving["id"]
    execution = again.start()
    again.wait_for(execution["id"], "completed")
    assert ("restore", saved) in runner.calls
    assert [argv[0] for argv in runner.commands()] == [PI_COMMAND]  # no clone and no setup again
