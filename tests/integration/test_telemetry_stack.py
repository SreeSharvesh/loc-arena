"""The company's telemetry in real containers: what reaches the store, who reads it, and what cannot.

Brings up the stack a run renders, with one play's secrets. A sandbox sends one log record over OTLP to the
company's Collector, as anything on agent-net can; the store's reader reads it back with SQL, in the store's
own container, since nothing else reaches the store. The store is then stopped, and the Collector still takes
a record. Last the store and the Collector are recreated with the honest twin's secrets, as its renewal does.
The play happens once, in a fixture, and each test reads what it saw.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml
from loc_arena.config import sandbox_service
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.telemetry_stack import COLLECTOR_SERVICE, USERS_VARIABLE

from tests.integration._docker_support import image_exists
from tests.integration.test_deploy_stack import AUTHOR, CONFIG, RUN, Stack

STORE = next(service for service in CONFIG.live_services if service.collects)
BODY = "one record of the reach test"
# In a sandbox: one log record over OTLP to the Collector, as JSON; prints the status, or the error's name.
SEND_RECORD = """
import json, sys, time, urllib.request
record = {"timeUnixNano": str(time.time_ns()), "severityText": "ERROR", "body": {"stringValue": sys.argv[2]}}
resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "probe"}}]}
logs = {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [record]}]}]}
request = urllib.request.Request(sys.argv[1], json.dumps(logs).encode(), {"content-type": "application/json"})
try:
    print(urllib.request.urlopen(request, timeout=10).status)
except OSError as error:
    print(type(error).__name__)
"""
RESOLVE = """
import socket, sys
try:
    socket.gethostbyname(sys.argv[1]); print("resolved")
except socket.gaierror:
    print("gaierror")
"""
SELECT_BODIES = "SELECT body FROM opentelemetry_logs"
ARRIVAL_SECONDS = 20  # how long a record may take from the Collector to the store
ANSWER_SECONDS = 10  # how long the Collector may take to answer with no store to send to

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason=f"needs Docker and the {CONFIG.settings.stack.image} image",
)


def read_passwords(play: dict[str, str]) -> dict[str, str]:
    """Each store user's password in ``play``, from the store's user file: ``user:mode=password``."""
    lines = play[USERS_VARIABLE].splitlines()
    return {line.split("=", 1)[0].split(":")[0]: line.split("=", 1)[1] for line in lines}


def query(stack: Stack, user: str, password: str, sql: str) -> dict[str, Any]:
    """What the store answers ``user`` for ``sql``, asked in its own container: its JSON, or its error."""
    url = f"http://localhost:{STORE.port}/v1/sql"
    curl = ["curl", "--silent", "--user", f"{user}:{password}", url, "--data-urlencode", f"sql={sql}"]
    return json.loads(stack.run("exec", "-T", STORE.name, *curl))


def read_bodies(answer: dict[str, Any]) -> list[str]:
    """The one column of each row of a ``SELECT`` the store answered; none for a table it does not hold."""
    return [row[0] for output in answer.get("output", []) for row in output["records"]["rows"]]


@dataclass
class Seen:
    """What one play saw: each answer, by the step it was the answer to, and how long some took."""

    answers: dict[str, Any] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)


@pytest.fixture(scope="module")
def seen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seen]:
    directory = tmp_path_factory.mktemp("telemetry-stack")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    attack, twin = issue_secrets(CONFIG)
    compose = ["docker", "compose", "-p", f"locarena-telemetry{secrets.token_hex(3)}"]
    compose += ["-f", str(directory / "compose.yaml"), "--project-directory", str(directory)]
    stack = Stack(compose, {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}, time.time())
    sandbox, passwords = sandbox_service(AUTHOR), read_passwords(attack)
    collector = f"http://{COLLECTOR_SERVICE}:{CONFIG.settings.telemetry.collector_port}/v1/logs"

    def send(body: str) -> str:
        return stack.run("exec", "-T", sandbox, "python", "-c", SEND_RECORD, collector, body).strip()

    say = (saw := Seen()).answers
    try:
        stack.run("up", "--detach", "--wait", "--no-build", STORE.name, COLLECTOR_SERVICE, sandbox)
        say["sent"] = send(BODY)
        deadline = time.monotonic() + ARRIVAL_SECONDS
        while not (read := read_bodies(query(stack, "reader", passwords["reader"], SELECT_BODIES))):
            assert time.monotonic() < deadline, "the record did not reach the store"
            time.sleep(1)
        say["read"] = read
        say["writer reads"] = query(stack, "collector", passwords["collector"], SELECT_BODIES)
        say["reader deletes"] = query(stack, "reader", passwords["reader"], "TRUNCATE opentelemetry_logs")
        say["read after"] = read_bodies(query(stack, "reader", passwords["reader"], SELECT_BODIES))
        say["no password"] = query(stack, "reader", "", SELECT_BODIES)
        for host in (STORE.name, COLLECTOR_SERVICE):
            say[f"resolves {host}"] = stack.run("exec", "-T", sandbox, "python", "-c", RESOLVE, host).strip()
        stack.run("stop", STORE.name)
        started = time.monotonic()
        say["sent with no store"] = send("never stored")
        saw.seconds["sent with no store"] = time.monotonic() - started
        # As the honest twin's renewal recreates them: empty, under the twin's secrets.
        stack.play = {**stack.play, **twin}
        recreate = ["up", "--detach", "--wait", "--force-recreate", "--renew-anon-volumes", "--no-deps"]
        stack.run(*recreate, STORE.name, COLLECTOR_SERVICE)
        say["twin reads"] = read_bodies(query(stack, "reader", read_passwords(twin)["reader"], SELECT_BODIES))
        # The twin's store holds no table yet, so each reader asks for what an empty store answers.
        say["twin's reader"] = read_bodies(query(stack, "reader", read_passwords(twin)["reader"], "SELECT 1"))
        say["attack's reader in the twin"] = query(stack, "reader", passwords["reader"], "SELECT 1")
        yield saw
    finally:
        stack.run("down", "--volumes", "--remove-orphans")


def test_a_record_sent_to_the_collector_is_read_from_the_store_by_its_reader(seen: Seen) -> None:
    assert (seen.answers["sent"], seen.answers["read"]) == ("200", [BODY])


def test_the_stores_writer_cannot_read_and_its_reader_cannot_write_and_no_password_reads_nothing(
    seen: Seen,
) -> None:
    refused = [seen.answers[step].get("error", "") for step in ("writer reads", "reader deletes")]

    assert all("User is not authorized to perform this action" in error for error in refused), refused
    assert "output" not in seen.answers["no password"]
    assert seen.answers["read after"] == [BODY]  # still there after the reader's TRUNCATE


def test_a_sandbox_resolves_the_collector_and_not_the_store(seen: Seen) -> None:
    resolved = (seen.answers[f"resolves {COLLECTOR_SERVICE}"], seen.answers[f"resolves {STORE.name}"])

    assert resolved == ("resolved", "gaierror")


def test_the_collector_takes_a_record_while_the_store_is_down(seen: Seen) -> None:
    assert seen.answers["sent with no store"] == "200"
    assert seen.seconds["sent with no store"] < ANSWER_SECONDS


def test_the_honest_twins_store_is_empty_and_refuses_the_attacks_reader(seen: Seen) -> None:
    assert (seen.answers["twin reads"], seen.answers["twin's reader"]) == ([], [1])
    assert "output" not in seen.answers["attack's reader in the twin"]
