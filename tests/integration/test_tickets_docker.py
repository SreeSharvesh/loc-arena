"""The tickets service in real containers under central IAM, as a stack run serves it.

Brings up the rendered compose project's tools gateway and the services on its route, and the model gateway
whose record ``renew_services`` drains (the images are built first, so they hold this code), then plays as
serving-agent from inside the tickets container, as the images have no curl and agent-net is isolated: it
finds its sprint ticket through the tools gateway with its sandbox token, works it and files another, and
calls the HTTP API with its credential on the service. Then the services are recreated as ``renew_services``
does before the honest twin, which must find the tracker as the seed left it.
Skipped unless the Docker daemon answers and the stack image exists.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import (
    REPOSITORY,
    issue_identities,
    issue_secrets,
    render_compose,
    token_variable,
)
from loc_arena.gateway.model_gateway import MODEL_GATEWAY_SERVICE
from loc_arena.recorded_events import ServiceRequestRecord
from loc_arena.stack_play import renew_services
from loc_arena.tool_records import ToolRecord
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE

from tests.integration._docker_support import image_exists

RUN = "aurora-efficiency.iam"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
AGENT = "serving-agent"
TIMEOUT = 900
SPRINT_TICKET = "AURORA-2"  # serving-agent's in the seed
FILED_TICKET = "SERVING-48"  # the one after the seed's SERVING-47
COMMENT = "Batching by sequence length; SERVING-31 first."

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason="needs a reachable Docker daemon and the stack image",
)

# Each script takes its credential on stdin, never in a command line. The engine's own MCP client, as the
# episode's: each call's structured result, or ``{"error": ...}``.
CALL_TOOLS = """
import json, sys
from loc_arena.scaffold.mcp_client import McpTools, over_http
url, calls, token = sys.argv[1], json.loads(sys.argv[2]), sys.stdin.read()
tools = McpTools(lambda: over_http(url, token, 30))
print(json.dumps([tools.call(name, arguments) for name, arguments in calls]))
"""
CALL_API = """
import json, sys, urllib.request, urllib.error
url, token = sys.argv[1], sys.stdin.read()
request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(request, timeout=10) as reply:
        print(json.dumps([reply.status, json.loads(reply.read())]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, None]))
"""
READ_HISTORY = """
import json, sqlite3, sys
query = "SELECT subject, action, field, old_value, new_value FROM history WHERE actor = ? ORDER BY id"
print(json.dumps(sqlite3.connect(sys.argv[1]).execute(query, (sys.argv[2],)).fetchall()))
"""
WORK = [
    ["search_tickets", {"assignee": AGENT}],
    ["edit_ticket", {"ticket": SPRINT_TICKET, "status": "in_progress"}],
    ["comment_on_ticket", {"ticket": SPRINT_TICKET, "body": COMMENT}],
    ["create_ticket", {"project": "SERVING", "title": "Bucket batches by length"}],
    ["get_ticket", {"ticket": SPRINT_TICKET}],
]
TWIN_LOOKS = [["get_ticket", {"ticket": SPRINT_TICKET}], ["get_ticket", {"ticket": FILED_TICKET}]]


@dataclass(frozen=True)
class Played:
    """What the stack left: the agent's results, the service's records and history, the twin's look."""

    worked: list[dict[str, Any]]
    api_with_credential: list[Any]
    api_with_a_guess: list[Any]
    records: list[ToolRecord]
    requests: list[ServiceRequestRecord]
    history: list[list[str | None]]
    twin_looked: list[dict[str, Any]]


@pytest.fixture(scope="module")
def played(tmp_path_factory: pytest.TempPathFactory) -> Played:
    directory = tmp_path_factory.mktemp("tickets-docker")
    compose_file = directory / "compose.yaml"
    compose_file.write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    project = f"locarena-tickets-{secrets.token_hex(3)}"
    command = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    command += ["--project-directory", str(REPOSITORY)]
    attack, twin = issue_secrets(CONFIG)
    attack["OPENROUTER_API_KEY"] = twin["OPENROUTER_API_KEY"] = "scripted-no-calls-dummy"
    environment, twin_environment = {**os.environ, **attack}, {**os.environ, **twin}
    stack = CONFIG.settings.stack
    tools_url = f"http://{TOOLS_GATEWAY_SERVICE}:{stack.tools_gateway_port}{MCP_PATH}"
    tickets = next(service for service in CONFIG.live_services if service.name == "tickets")
    identity = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == (tickets.name, AGENT))

    def compose(*arguments: str, play: dict[str, str] = environment, stdin: str | None = None) -> str:
        """The output of ``docker compose`` with ``arguments`` and ``stdin``, in ``play``'s environment."""
        done = subprocess.run(
            [*command, *arguments],
            input=stdin,
            env=play,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        assert done.returncode == 0, done.stderr[-3000:]
        return done.stdout

    def run_in_tickets(
        script: str,
        *arguments: str,
        credential: str = "",
        play: dict[str, str] = environment,
    ) -> Any:  # noqa: ANN401
        """What ``script`` prints last, run in ``play``'s tickets container, ``credential`` on its stdin."""
        python = ["exec", "-T", tickets.name, "python", "-c", script]
        return json.loads(compose(*python, *arguments, play=play, stdin=credential).strip().splitlines()[-1])

    try:
        compose("build")
        compose("up", "--detach", "--wait", TOOLS_GATEWAY_SERVICE, MODEL_GATEWAY_SERVICE)
        worked = run_in_tickets(
            CALL_TOOLS,
            tools_url,
            json.dumps(WORK),
            credential=attack[token_variable(AGENT)],
        )
        ticket_url = f"http://{tickets.name}:{tickets.port}/tickets/{SPRINT_TICKET}"
        api_with_credential = run_in_tickets(CALL_API, ticket_url, credential=attack[identity.variable])
        api_with_a_guess = run_in_tickets(CALL_API, ticket_url, credential="guessed")
        history = run_in_tickets(READ_HISTORY, str(CONFIG.settings.tickets.database), AGENT)
        service_logs = directory / "services"
        service_logs.mkdir()
        renew_services(CONFIG, command, service_logs, environment, twin_environment)
        twin_looked = run_in_tickets(
            CALL_TOOLS,
            tools_url,
            json.dumps(TWIN_LOOKS),
            credential=twin[token_variable(AGENT)],
            play=twin_environment,
        )
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            env=environment,
            capture_output=True,
        )
    lines = [
        json.loads(line)
        for line in (service_logs / "tickets.log").read_text().splitlines()
        if line[:1] == "{"
    ]
    return Played(
        worked=worked,
        api_with_credential=api_with_credential,
        api_with_a_guess=api_with_a_guess,
        records=[ToolRecord.model_validate(line) for line in lines if "operation" not in line],
        requests=[ServiceRequestRecord.model_validate(line) for line in lines if "operation" in line],
        history=history,
        twin_looked=twin_looked,
    )


def test_an_agent_finds_works_and_files_tickets_through_the_tools_gateway(played: Played) -> None:
    found, edited, commented, filed, read = played.worked

    assert [ticket["id"] for ticket in found["tickets"]] == [SPRINT_TICKET]
    assert (edited["status"], commented["author"], filed["id"], filed["creator"]) == (
        "in_progress",
        AGENT,
        FILED_TICKET,
        AGENT,
    )
    assert [(comment["author"], comment["body"]) for comment in read["comments"]] == [(AGENT, COMMENT)]


def test_the_services_record_names_the_agent_and_the_iam_decision_of_each_call(played: Played) -> None:
    assert [(record.agent, record.tool, record.error) for record in played.records] == [
        (AGENT, name, None) for name, _ in WORK
    ]
    assert all(record.iam_decision for record in played.records)


def test_the_http_api_serves_the_agent_its_credential_names_and_refuses_a_guess(played: Played) -> None:
    status, ticket = played.api_with_credential

    assert (status, ticket["status"], played.api_with_a_guess[0]) == (
        HTTPStatus.OK,
        "in_progress",
        HTTPStatus.UNAUTHORIZED,
    )
    assert [(request.caller, request.operation, request.status) for request in played.requests] == [
        (AGENT, "get_ticket", HTTPStatus.OK),
        (None, "get_ticket", HTTPStatus.UNAUTHORIZED),
    ]


def test_the_history_in_the_container_holds_each_change_the_agent_made(played: Played) -> None:
    assert [row[:4] for row in played.history] == [
        [SPRINT_TICKET, "changed", "status", "open"],
        [SPRINT_TICKET, "commented", "comment", None],
        [FILED_TICKET, "created", None, None],
    ]


def test_the_honest_twin_finds_the_tracker_as_the_seed_left_it(played: Played) -> None:
    sprint, filed = played.twin_looked

    assert (sprint["status"], sprint["comments"]) == ("open", [])
    assert f"no ticket {FILED_TICKET}" in filed["error"]
