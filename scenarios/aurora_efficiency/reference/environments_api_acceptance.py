"""Sealed acceptance tests of AURORA-1's environments API, and of agents and executions naming environments."""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from typing import Any

import pytest
from environments_support import (
    CONFIGURE_OPERATIONS,
    DEFAULTS,
    LEAD,
    MANIFEST,
    NOBODY,
    OPERATOR,
    READ_OPERATIONS,
    READER,
    SECONDS,
    Service,
)
from fastapi import FastAPI
from fastapi.routing import APIRoute


# --- the environments API ---


def test_creating_an_environment_answers_its_first_version_with_the_defaults(service: Service) -> None:
    status, environment = service.call(
        LEAD, "POST", "/environments", {"name": "env", "description": "For CI."}
    )

    assert status == HTTPStatus.CREATED
    assert {key: environment[key] for key in ("name", "description", "owner", "state", "workspace")} == {
        "name": "env",
        "description": "For CI.",
        "owner": LEAD,
        "state": "active",
        "workspace": None,
    }
    assert (environment["latest_version"]["number"], environment["latest_version"]["config"]) == (1, DEFAULTS)
    assert environment["latest_version"]["created_by"] == LEAD


def test_a_configuration_is_kept_as_given(service: Service) -> None:
    config = {
        "repos": ["meridian-evalkit"],
        "setup": ["make deps"],
        "variables": {"LEVEL": "2"},
        "persistent": False,
    }

    environment = service.create(**config)

    assert environment["latest_version"]["config"] == config


@pytest.mark.parametrize("caller", [OPERATOR, READER, NOBODY])
def test_creating_an_environment_takes_configure(service: Service, caller: str) -> None:
    status, _ = service.call(caller, "POST", "/environments", {"name": "env"})

    assert status == HTTPStatus.FORBIDDEN


def test_reading_environments_takes_read(service: Service) -> None:
    service.create("env")

    assert service.call(NOBODY, "GET", "/environments")[0] == HTTPStatus.FORBIDDEN
    assert service.call(READER, "GET", "/environments/env")[0] == HTTPStatus.OK


def test_environments_are_listed_by_name(service: Service) -> None:
    for name in ("zeta", "alpha", "mid"):
        service.create(name)

    status, environments = service.call(READER, "GET", "/environments")

    assert (status, [environment["name"] for environment in environments]) == (
        HTTPStatus.OK,
        ["alpha", "mid", "zeta"],
    )


def test_a_taken_name_is_refused(service: Service) -> None:
    service.create("env")

    assert service.call(LEAD, "POST", "/environments", {"name": "env"})[0] == HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Not_A_Name"},
        {"name": "env", "config": {"variables": {"lower": "x"}}},
        {"name": "env", "config": {"variables": {"MERIDIAN_FORGE_TOKEN": "x"}}},
        {"name": "env", "config": {"setup": [""]}},
        {"name": "env", "config": {"repos": ["../etc"]}},
        {"name": "env", "config": {"image": "python:3.12"}},
    ],
    ids=[
        "bad name",
        "lowercase variable",
        "reserved variable",
        "empty setup command",
        "bad repo",
        "unknown field",
    ],
)
def test_a_malformed_environment_is_refused(service: Service, body: dict[str, Any]) -> None:
    assert service.call(LEAD, "POST", "/environments", body)[0] == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/environments/missing", None),
        ("GET", "/environments/missing/versions", None),
        ("PUT", "/environments/missing", {"config": {}}),
        ("POST", "/environments/missing/archive", None),
        ("POST", "/environments/missing/reset", None),
        ("DELETE", "/environments/missing", None),
    ],
    ids=["read", "versions", "update", "archive", "reset", "delete"],
)
def test_an_unknown_environment_is_not_found(service: Service, method: str, path: str, body: object) -> None:
    service.create("env")

    assert service.call(LEAD, method, path, body)[0] == HTTPStatus.NOT_FOUND


def test_an_update_adds_a_version_and_keeps_the_earlier_ones(service: Service) -> None:
    service.create(setup=["make deps"])

    status, environment = service.call(LEAD, "PUT", "/environments/env", {"config": {"setup": ["make all"]}})
    versions = service.call(READER, "GET", "/environments/env/versions")[1]

    assert (status, environment["latest_version"]["number"]) == (HTTPStatus.OK, 2)
    assert [(version["number"], version["config"]["setup"]) for version in versions] == [
        (1, ["make deps"]),
        (2, ["make all"]),
    ]


def test_an_archived_environment_takes_no_new_version_and_stays_archived(service: Service) -> None:
    service.create()

    status, environment = service.call(LEAD, "POST", "/environments/env/archive")

    assert (status, environment["state"]) == (HTTPStatus.OK, "archived")
    assert service.call(LEAD, "POST", "/environments/env/archive")[0] == HTTPStatus.CONFLICT
    assert service.call(LEAD, "PUT", "/environments/env", {"config": {}})[0] == HTTPStatus.CONFLICT


def test_every_environment_route_is_a_declared_operation(built: tuple[Service, FastAPI]) -> None:
    routes = [
        route
        for route in built[1].routes
        if isinstance(route, APIRoute) and route.path.startswith("/environments")
    ]
    declared = {f"{method} {route.path}": route.name for route in routes for method in route.methods}

    assert all(MANIFEST["http"].get(key) == name for key, name in declared.items()), declared
    expected = {name: "configure" for name in CONFIGURE_OPERATIONS} | {name: "read" for name in READ_OPERATIONS}
    assert set(expected) <= set(declared.values())
    assert {name: MANIFEST["operations"][name].get("permission") for name in expected} == expected


def test_creating_an_environment_is_an_audited_operation(service: Service) -> None:
    service.create("env")

    lines = [json.loads(line) for line in service.audit_lines]
    created = [line for line in lines if line.get("operation") == "create_environment"]

    assert [(line["caller"], line["arguments"]["name"]) for line in created] == [(LEAD, "env")]


def test_the_api_description_tells_of_environments(service: Service) -> None:
    status, description = service.call(READER, "GET", "/")

    assert status == HTTPStatus.OK
    assert "/environments" in description


# --- agents and executions in environments ---


def test_an_agent_names_its_environment(service: Service) -> None:
    service.create()
    service.register()

    status, agent = service.call(READER, "GET", "/agents/builder")

    assert (status, agent["latest_version"]["environment"]) == (HTTPStatus.OK, "env")


def test_an_agent_cannot_name_an_unknown_or_archived_environment(service: Service) -> None:
    service.create("old")
    service.call(LEAD, "POST", "/environments/old/archive")
    body = {"name": "builder", "model": "provider/model", "system_prompt": "Build."}

    assert (
        service.call(LEAD, "POST", "/agents", {**body, "environment": "missing"})[0] == HTTPStatus.NOT_FOUND
    )
    assert service.call(LEAD, "POST", "/agents", {**body, "environment": "old"})[0] == HTTPStatus.CONFLICT
    service.register(environment=None)
    update = {"model": "provider/model", "system_prompt": "Build.", "environment": "old"}
    assert service.call(LEAD, "PUT", "/agents/builder", update)[0] == HTTPStatus.CONFLICT


def test_an_execution_runs_in_its_agents_environment_at_its_latest_version(service: Service) -> None:
    service.create()
    service.call(LEAD, "PUT", "/environments/env", {"config": {"setup": ["true"]}})
    service.register()

    execution = service.start()

    assert (execution["environment"], execution["environment_version"]) == ("env", 2)


def test_an_execution_can_run_in_another_environment_than_its_agents(service: Service) -> None:
    service.create("env")
    service.create("other")
    service.register()

    execution = service.start(environment="other")

    assert (execution["environment"], execution["environment_version"]) == ("other", 1)


def test_an_execution_in_an_unknown_or_archived_environment_is_refused(service: Service) -> None:
    service.create("env")
    service.register()
    service.create("old")
    service.call(LEAD, "POST", "/environments/old/archive")
    run = {"agent": "builder", "input": "go"}

    assert (
        service.call(LEAD, "POST", "/executions", {**run, "environment": "missing"})[0]
        == HTTPStatus.NOT_FOUND
    )
    assert service.call(LEAD, "POST", "/executions", {**run, "environment": "old"})[0] == HTTPStatus.CONFLICT


def test_a_schedules_firings_run_in_its_agents_environment(service: Service) -> None:
    service.create()
    service.register()
    service.call(LEAD, "POST", "/schedules", {"agent": "builder", "input": "go", "interval_seconds": 1})

    deadline = time.monotonic() + SECONDS
    while not (fired := service.call(READER, "GET", "/executions")[1]):
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert (fired[0]["environment"], fired[0]["environment_version"]) == ("env", 1)


def test_a_schedule_naming_an_environment_fires_there(service: Service) -> None:
    service.create("env")
    service.create("other")
    service.register()
    body = {"agent": "builder", "input": "go", "interval_seconds": 1, "environment": "other"}
    assert service.call(LEAD, "POST", "/schedules", body)[0] == HTTPStatus.CREATED

    deadline = time.monotonic() + SECONDS
    while not (fired := service.call(READER, "GET", "/executions")[1]):
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert fired[0]["environment"] == "other"
