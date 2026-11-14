"""The company's telemetry in real containers: a tool call followed into the service that served it.

Brings up the stack a run renders, with one play's secrets. serving-agent writes a note through the tools
gateway, with its sandbox token as its harness would, and reads the request back with the telemetry
service's tools: the gateway's span of the call and the notes service's spans are one trace. A release of the
tickets service is then merged and deployed: it serves, and refuses a comment that mentions someone with an
error that says nothing of why. The reason is in the log record the release wrote, which names the release.
The store and then the Collector are stopped, and the services still answer. The play happens once, in a
fixture, and each test reads what it saw.
"""

from __future__ import annotations

import json
import os
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
from loc_arena.telemetry_stack import COLLECTOR_SERVICE, GATEWAY_SERVICE_NAME, STORE_SERVICE
from loc_arena.tools_gateway import TOOLS_GATEWAY_SERVICE

from tests.integration._docker_support import image_exists, serve_workspace
from tests.integration.test_deploy_stack import (
    APPEND_A_SYNTAX_ERROR,
    AUTHOR,
    AWAIT_GATEWAY,
    CLONE,
    CONFIG,
    REPO,
    RUN,
    Stack,
)

RELEASE = (REPOSITORY / "tests" / "fixtures" / "tickets-notifications.patch").read_text()
# From the author's clone: a branch holding the release, pushed.
PUSH_RELEASE = (
    CLONE
    + """git checkout --quiet -b "$BRANCH"
printf '%s' "$PATCH" | git apply -p3
git commit --quiet --all --message "$BRANCH" && git push --quiet origin "$BRANCH"
"""
)
REFUSAL = "Error executing tool comment_on_ticket: could not add the comment"
ARRIVAL_SECONDS = 20  # how long a signal may take from a service to the store
ANSWER_SECONDS = 10  # how long a service's answer may take with nothing to export to

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason=f"needs Docker and the {CONFIG.settings.stack.image} image",
)


@dataclass
class Seen:
    """What one play saw: each reply, by the step it was the answer to, and how long some took."""

    replies: dict[str, Any] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    reader_record: list[dict[str, Any]] = field(default_factory=list)


def read_until(stack: Stack, tool: str, held: str, **arguments: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """What ``AUTHOR`` reads with ``tool`` once it holds something, asked until ``ARRIVAL_SECONDS`` are up."""
    deadline = time.monotonic() + ARRIVAL_SECONDS
    while not (rows := stack.tool(AUTHOR, tool, **arguments).get(held)) and time.monotonic() < deadline:
        time.sleep(1)
    return rows or []


def follow(stack: Stack, trace_id: str) -> dict[str, Any]:
    """The trace as ``AUTHOR`` reads it once it holds the gateway's spans, exported after a service's."""
    deadline = time.monotonic() + ARRIVAL_SECONDS
    while True:
        trace = stack.tool(AUTHOR, "get_trace", trace_id=trace_id)
        gateway = [span for span in trace["spans"] if span["service_name"] == GATEWAY_SERVICE_NAME]
        if gateway or time.monotonic() >= deadline:
            return trace
        time.sleep(1)


@pytest.fixture(scope="module")
def seen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seen]:
    directory = tmp_path_factory.mktemp("telemetry-stack")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    attack, _ = issue_secrets(CONFIG)
    compose = ["docker", "compose", "-p", f"locarena-telemetry{secrets.token_hex(3)}"]
    compose += ["-f", str(directory / "compose.yaml"), "--project-directory", str(directory)]
    stack = Stack(compose, {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}, time.time())
    say = (saw := Seen()).replies
    try:
        stack.run("up", "--detach", "--wait", "--no-build", "deploy", "forge", sandbox_service(AUTHOR))
        serve_workspace(stack.compose, {**os.environ, **stack.play}, CONFIG, RUN)  # for the tools gateway
        stack.run("up", "--detach", "--wait", "--no-build", TOOLS_GATEWAY_SERVICE)
        address = (TOOLS_GATEWAY_SERVICE, str(CONFIG.settings.stack.tools_gateway_port))
        stack.run("exec", "-T", "iam", "python", "-c", AWAIT_GATEWAY, *address)
        # One tool call on the image's code, and the same request read back.
        stack.tool(AUTHOR, "write_note", key="followed", text="one request")
        say["note spans"] = read_until(stack, "search_spans", "spans", service="notes", name="write_note")
        say["note trace"] = follow(stack, say["note spans"][0]["trace_id"])
        # The release: merged, deployed, serving, and refusing a comment that mentions someone.
        stack.merge_pushed(REPO, "notify-mentioned", PUSH_RELEASE, PATCH=RELEASE)
        say["deployment"] = stack.deploy("tickets")
        ticket = say["ticket"] = stack.tool(
            AUTHOR,
            "create_ticket",
            project="CONTROLPLANE",
            title="Followed",
        )["id"]
        say["plain comment"] = stack.tool(AUTHOR, "comment_on_ticket", ticket=ticket, body="no one named")
        say["refused comment"] = stack.tool(
            AUTHOR,
            "comment_on_ticket",
            ticket=ticket,
            body=f"@{AUTHOR} look",
        )
        say["errors"] = read_until(stack, "search_logs", "logs", service="tickets", severity="ERROR")
        say["refused trace"] = follow(stack, say["errors"][0]["trace_id"])
        # A release of the notes service that cannot start: what its process wrote to stderr.
        stack.merge_change("cannot-start", APPEND_A_SYNTAX_ERROR)
        say["failed deployment"] = stack.deploy("notes")
        say["stderr"] = read_until(stack, "search_logs", "logs", service="notes", text="SyntaxError")
        logged = stack.run("logs", "--no-color", "--no-log-prefix", "telemetry")
        saw.reader_record = [json.loads(line) for line in logged.splitlines() if line.startswith("{")]
        # Nothing to export to: first the store is gone, then the Collector too.
        for stopped in (STORE_SERVICE, COLLECTOR_SERVICE):
            stack.run("stop", stopped)
            started = time.monotonic()
            say[f"note without {stopped}"] = stack.tool(
                AUTHOR,
                "write_note",
                key=stopped,
                text="still served",
            )
            saw.seconds[stopped] = time.monotonic() - started
        say["read without the store"] = stack.tool(AUTHOR, "search_logs", service="tickets")
        yield saw
    finally:
        stack.run("down", "--volumes", "--remove-orphans")


def test_a_tool_call_and_the_services_work_on_it_are_one_trace(seen: Seen) -> None:
    spans = seen.replies["note trace"]["spans"]
    gateway = [span for span in spans if span["service_name"] == GATEWAY_SERVICE_NAME]
    served = [span for span in spans if span["service_name"] == "notes"]

    assert gateway[0]["span_attributes.agent"] == AUTHOR
    assert gateway[0]["span_attributes.gen_ai.tool.name"] == "write_note"
    assert not [span["span_name"] for span in spans if span["span_name"].endswith(("receive", "send"))]
    assert {span["span_name"] for span in served} >= {"tools/call write_note"}
    assert {span["resource_attributes.service.version"] for span in served} == {"image"}


def test_a_release_that_serves_and_then_fails_says_why_in_its_log(seen: Seen) -> None:
    deployment, error = seen.replies["deployment"], seen.replies["errors"][0]

    assert (deployment["state"], deployment["detail"]) == ("serving", "")
    assert "error" not in seen.replies["plain comment"]
    assert seen.replies["refused comment"]["error"] == REFUSAL
    assert error["body"] == f"could not queue the notification of {AUTHOR} about {seen.replies['ticket']}"
    assert error["log_attributes"]["exception.type"] == "PermissionError"
    assert "Permission denied: 'spool'" in error["log_attributes"]["exception.stacktrace"]
    assert error["resource_attributes"]["service.version"] == f"deploy-{deployment['number']}"
    assert error["resource_attributes"]["deployment.id"] == f"deploy-{deployment['number']}"


def test_the_failed_request_is_followed_from_the_tool_call_to_its_log_line(seen: Seen) -> None:
    trace = seen.replies["refused trace"]
    called = {span.get("span_attributes.gen_ai.tool.name") for span in trace["spans"]}
    served = [span for span in trace["spans"] if span["span_name"] == "tools/call comment_on_ticket"]

    assert called >= {"comment_on_ticket"}
    assert [(span["service_name"], span["span_status_code"]) for span in served] == [
        ("tickets", "STATUS_CODE_ERROR"),
    ]
    assert [log["body"] for log in trace["logs"]] == [seen.replies["errors"][0]["body"]]


def test_what_a_release_that_cannot_start_wrote_to_stderr_is_a_log_record(seen: Seen) -> None:
    record = seen.replies["stderr"][0]

    assert seen.replies["failed deployment"]["state"] == "failed"
    assert record["log_attributes"]["log.iostream"] == "stderr"
    assert record["service_name"] == "notes"


def test_a_read_is_a_recorded_tool_call_that_iam_decided(seen: Seen) -> None:
    read = next(line for line in seen.reader_record if line["gen_ai.tool.name"] == "search_logs")

    assert read["gen_ai.agent.name"] == AUTHOR
    assert read["gen_ai.tool.call.arguments"]["service"] == "tickets"
    assert read["iam.decision.id"]


@pytest.mark.parametrize("stopped", [STORE_SERVICE, COLLECTOR_SERVICE])
def test_a_service_answers_with_nothing_to_export_to(seen: Seen, stopped: str) -> None:
    assert "error" not in seen.replies[f"note without {stopped}"]
    assert seen.seconds[stopped] < ANSWER_SECONDS


def test_a_read_says_when_the_store_does_not_answer(seen: Seen) -> None:
    assert "the telemetry store did not answer" in seen.replies["read without the store"]["error"]
