"""The company's telemetry's bounds, in real containers: retention, the store's disk, the Collector's memory.

Brings up the store and the Collector a run renders, twice, with one play's secrets and bounds small enough to
reach: first the store's disk, which records sent until it is full fill; then the Collector's memory, set
below what the Collector itself holds, so it refuses every record. Each time a sandbox sends the records, as
anything on agent-net can, and the store's reader reads them back in the store's own container.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from loc_arena.config import RunConfig, sandbox_service
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.telemetry_stack import COLLECTOR_SERVICE, STORE_DATA, find_store

from tests.integration._docker_support import needs_stack
from tests.integration.test_deploy_stack import AUTHOR, CONFIG, RUN, Stack
from tests.integration.test_telemetry_stack import SEND_RECORD, query, read_passwords, read_rows

STORE = find_store(CONFIG)
DISK_MIB = 16  # small enough to fill in seconds
RECORD_BYTES = 1 << 20  # each record's body
SENT = 40  # records sent at the full store: more than its disk holds
# The shared sender, with a body ``x<n>`` of ``n`` random hexadecimal digits made in the sandbox, which no
# compression shrinks.
SIZED_BODY = 'os.urandom(int(sys.argv[2][1:]) // 2).hex() if sys.argv[2][0] == "x" else sys.argv[2]'
SEND_SIZED = SEND_RECORD.replace("import json,", "import os, json,").replace(
    '"body": {"stringValue": sys.argv[2]}',
    f'"body": {{"stringValue": {SIZED_BODY}}}',
)
# In a sandbox: one span over OTLP to the Collector, as JSON.
SEND_SPAN = """
import json, os, sys, time, urllib.request
now = time.time_ns()
span = {"traceId": os.urandom(16).hex(), "spanId": os.urandom(8).hex(), "name": "probe", "kind": 2,
        "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000)}
resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "probe"}}]}
body = {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [span]}]}]}
request = urllib.request.Request(sys.argv[1], json.dumps(body).encode(), {"content-type": "application/json"})
urllib.request.urlopen(request, timeout=10)
"""
ARRIVAL_SECONDS = 20  # how long a record may take from the Collector to the store

pytestmark = [
    *needs_stack(CONFIG.settings.stack.image),
    pytest.mark.skipif(STORE is None, reason="needs a telemetry store"),
]


def bounded(**telemetry: object) -> RunConfig:
    """The run's config with these ``telemetry:`` settings."""
    settings = CONFIG.settings.telemetry.model_copy(update=telemetry)
    return dataclasses.replace(CONFIG, settings=CONFIG.settings.model_copy(update={"telemetry": settings}))


@pytest.fixture
def stack(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Stack]:
    """The store, the Collector and one sandbox of a run under ``request.param``'s config."""
    config: RunConfig = request.param
    (tmp_path / "compose.yaml").write_text(yaml.safe_dump(render_compose(config, REPOSITORY, RUN, [])))
    attack, _ = issue_secrets(config)
    compose = ["docker", "compose", "-p", f"locarena-bounds{secrets.token_hex(3)}"]
    compose += ["-f", str(tmp_path / "compose.yaml"), "--project-directory", str(tmp_path)]
    running = Stack(compose, {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}, time.time())
    try:
        assert STORE is not None
        running.run(
            "up",
            "--detach",
            "--wait",
            "--no-build",
            STORE.name,
            COLLECTOR_SERVICE,
            sandbox_service(AUTHOR),
        )
        yield running
    finally:
        running.run("down", "--volumes", "--remove-orphans")


def send(stack: Stack, body: str) -> str:
    """What the Collector answers a sandbox sending a log record of ``body``: a status or an error's name.

    A body ``x<n>`` is ``n`` random digits, made in the sandbox, so no command line carries it.
    """
    collector = f"http://{COLLECTOR_SERVICE}:{CONFIG.settings.telemetry.collector_port}/v1/logs"
    script = ["python", "-c", SEND_SIZED, collector, body]
    return stack.run("exec", "-T", sandbox_service(AUTHOR), *script).strip()


def send_span(stack: Stack) -> None:
    """A sandbox sends one span over OTLP to the Collector."""
    collector = f"http://{COLLECTOR_SERVICE}:{CONFIG.settings.telemetry.collector_port}/v1/traces"
    stack.run("exec", "-T", sandbox_service(AUTHOR), "python", "-c", SEND_SPAN, collector)


def ask(stack: Stack, sql: str) -> dict[str, Any]:
    """What the store answers its reader for ``sql``."""
    return query(stack, "reader", read_passwords(stack.play)["reader"], sql)


@pytest.mark.parametrize("stack", [bounded(store_disk_mib=DISK_MIB, retention="2h")], indirect=True)
def test_the_store_keeps_records_for_the_retention_and_on_a_full_disk_the_collector_still_answers(
    stack: Stack,
) -> None:
    assert STORE is not None
    first = send(stack, "the first record")
    deadline = time.monotonic() + ARRIVAL_SECONDS
    while "output" not in ask(stack, "SHOW CREATE TABLE opentelemetry_logs"):
        assert time.monotonic() < deadline, "the first record did not reach the store"
        time.sleep(1)
    created = json.dumps(ask(stack, "SHOW CREATE TABLE opentelemetry_logs"))
    send_span(stack)
    deadline = time.monotonic() + ARRIVAL_SECONDS
    while "output" not in ask(stack, "SHOW CREATE TABLE opentelemetry_traces"):
        assert time.monotonic() < deadline, "the span did not reach the store"
        time.sleep(1)
    traced = json.dumps(ask(stack, "SHOW CREATE TABLE opentelemetry_traces"))
    answers = [send(stack, f"x{RECORD_BYTES}") for _ in range(SENT)]
    time.sleep(ARRIVAL_SECONDS)  # every batch the Collector took has been sent, or dropped
    size, used = stack.run("exec", "-T", STORE.name, "df", "--output=size,used", STORE_DATA).split()[-2:]
    logged = stack.run("logs", "--no-color", STORE.name)

    assert first == "200"
    assert "ttl = '2h'" in created
    assert "ttl = '2h'" in traced
    assert set(answers) == {"200"}  # the Collector answers whatever the store took
    assert int(size) <= DISK_MIB * 1024  # the store's disk, in KiB
    assert int(used) >= int(size) * 9 // 10  # filled
    assert "log file allocation failed: fallocate" in logged  # the store refused writes for want of disk
    kept = read_rows(ask(stack, "SELECT count(*) AS kept FROM opentelemetry_logs"))[0]["kept"]
    assert 1 <= kept < 1 + SENT  # the store answers, and kept what its disk held


@pytest.mark.parametrize(
    "stack",
    [bounded(collector_memory_mib=2, collector_spike_mib=1)],
    indirect=True,
)
def test_a_collector_past_its_memory_bound_refuses_records_and_keeps_running(stack: Stack) -> None:
    answers = {send(stack, "refused") for _ in range(3)}
    running = stack.run("ps", "--status", "running", "--services")

    assert "200" not in answers
    assert COLLECTOR_SERVICE in running.split()
