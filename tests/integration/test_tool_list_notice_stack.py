"""A session that missed a service's tools, in real containers: it gets them back once the service serves.

Brings up ``test_deploy_stack``'s stack, with the workspace service and the tool-list notifier served as a
play serves them. controlplane-agent deploys a release of the notes service that starts slowly, and while it
starts, serving-agent opens a session through the tools gateway as pi does: the 2025 protocol, a stream for
notices. The session lists its tools, and lists them again each time it is told the list changed. The chat
service is stopped once the session has listed, and started again only after the deployment ended, so a
notice sent before every tool is offered again would be seen. Then the run's check after play reads the
gateway's log, and an agent's own key is tried on the notifier.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from loc_arena.episode_stack import token_variable
from loc_arena.recorded_events import SERVICE_LOGS
from loc_arena.stack_play import EPISODE_SERVICE
from loc_arena.tool_list_notifier import NOTIFIER_KEY_VARIABLE, TOOL_LIST_NOTIFIER
from loc_arena.tool_offer import LACKED_TOOLS_FILE, name_tools, report_lacked_tools
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE
from loc_arena.workspace_service import workspace_key

from tests.integration.test_deploy_stack import (  # noqa: F401 - the fixture
    AUTHOR,
    CONFIG,
    DEPLOYER,
    GATEWAY,
    NOTES_MODULE,
    SETTLE_SECONDS,
    Stack,
    pytestmark,
    stack,
)

START_SECONDS = 8  # how long the release takes to start: the notes service answers nothing meanwhile
START_SLOWLY = f"import sys; open(sys.argv[1], 'a').write('import time\\ntime.sleep({START_SECONDS})\\n')"
STARTED = "started the service on deploy-1"  # the supervisor's line: the release runs, answering nothing
NOTES_TOOLS = set(next(a for a in CONFIG.agents if a.id == AUTHOR).sandbox.tools["notes"])
# How long chat stays down after the deployment ended: a read of the records, then two checks of the gateway.
STACK_SETTINGS = CONFIG.settings.stack
DOWN_SECONDS = CONFIG.settings.pi.notice_poll_seconds + 2 * STACK_SETTINGS.gateway_health_interval_seconds
NAMED = name_tools(CONFIG)[AUTHOR]  # every tool the author's run config names
# The longest a session waits to be told: the deployment's end, then the gateway's check that all is offered.
LISTEN_SECONDS = SETTLE_SECONDS + CONFIG.settings.stack.tools_offer_seconds
NOTIFIER = f"http://{EPISODE_SERVICE}:{CONFIG.settings.stack.workspace_port}/{TOOL_LIST_NOTIFIER}{MCP_PATH}"
# In IAM's container, as the agent whose sandbox token is on stdin, as pi holds a session: it lists the tools,
# then lists again on each notice the list changed, until it holds every tool named or the time is up. Each
# listing is a line of the tools' names.
LISTEN = """
import anyio, httpx2, json, sys, time
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ToolListChangedNotification
async def listen(url, token, named, seconds):
    deadline, told, listings = time.monotonic() + seconds, [anyio.Event()], []
    async def on_message(message):
        if isinstance(message, ToolListChangedNotification):
            told[-1].set()
    transport = lambda http: streamable_http_client(url, http_client=http)
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=seconds) as http,
        Client(transport(http), mode="legacy", message_handler=on_message) as client,
    ):
        while True:
            listings.append(sorted(tool.name for tool in (await client.list_tools()).tools))
            print(json.dumps(listings[-1]), flush=True)
            if set(named) <= set(listings[-1]):
                return
            with anyio.move_on_after(deadline - time.monotonic()):
                await told[-1].wait()
            if not told[-1].is_set():
                return
            told.append(anyio.Event())
url, named, seconds = sys.argv[1], json.loads(sys.argv[2]), float(sys.argv[3])
anyio.run(listen, url, sys.stdin.read(), named, seconds)
"""
# In IAM's container: the HTTP status the notifier answers an initialize carrying the key on stdin.
OPEN_DIRECTLY = """
import json, sys, urllib.error, urllib.request
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})
headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
headers["authorization"] = "Bearer " + sys.stdin.read()
request = urllib.request.Request(sys.argv[1], body.encode(), headers)
try:
    with urllib.request.urlopen(request, timeout=30) as reply:
        print(reply.status)
except urllib.error.HTTPError as refused:
    print(refused.code)
"""


@dataclass
class Listened:
    """What one session listed across a deployment, what the run says of it, and whom the notifier admits."""

    listings: list[list[str]]
    lacked: list[dict[str, Any]]
    opened: dict[str, int]  # the status the notifier answered each key


@pytest.fixture(scope="module")
def listened(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> Listened:  # noqa: F811 - the fixture
    stack.merge_change("slow", START_SLOWLY, module=NOTES_MODULE)
    stack.tool(DEPLOYER, "deploy", service="notes")
    deadline = time.monotonic() + SETTLE_SECONDS
    while STARTED not in stack.run("logs", "--no-log-prefix", "notes"):
        assert time.monotonic() < deadline, "the hand-over did not start"
        time.sleep(0.5)
    script = ["python", "-c", LISTEN, GATEWAY, json.dumps(sorted(NAMED)), str(LISTEN_SECONDS)]
    session = subprocess.Popen(
        [*stack.compose, "exec", "-T", "iam", *script],
        env={**os.environ, **stack.play},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert session.stdin is not None and session.stdout is not None
    session.stdin.write(stack.play[token_variable(AUTHOR)])
    session.stdin.close()
    listings = [json.loads(session.stdout.readline())]
    stack.run("stop", "chat")
    assert stack.settle("notes")["state"] == "serving"
    time.sleep(DOWN_SECONDS)
    stack.run("start", "chat")
    listings += [json.loads(line) for line in session.stdout]
    assert session.wait() == 0
    run = tmp_path_factory.mktemp("run")
    (run / SERVICE_LOGS).mkdir()
    gateway_log = stack.run("logs", "--no-log-prefix", TOOLS_GATEWAY_SERVICE)
    (run / SERVICE_LOGS / f"{TOOLS_GATEWAY_SERVICE}.log").write_text(gateway_log)
    report_lacked_tools(CONFIG, run)
    keys = {
        "own workspace key": workspace_key(stack.play[token_variable(AUTHOR)]),
        "no key": "",
        "the gateway's key": stack.play[NOTIFIER_KEY_VARIABLE],
    }
    opened = {
        name: int(stack.run("exec", "-T", "iam", "python", "-c", OPEN_DIRECTLY, NOTIFIER, standard_input=key))
        for name, key in keys.items()
    }
    lacked = [json.loads(line) for line in Path(run / LACKED_TOOLS_FILE).read_text().splitlines()]
    return Listened(listings, lacked, opened)


def test_a_session_opened_while_notes_restarts_is_offered_notes_tools_once_notes_serves_again(
    listened: Listened,
) -> None:
    first, last = listened.listings[0], listened.listings[-1]

    assert (NOTES_TOOLS & set(first), NAMED - set(last)) == (set(), set())


def test_a_session_told_to_list_again_is_never_told_while_a_tool_it_names_is_missing(
    listened: Listened,
) -> None:
    told = listened.listings[1:]

    assert [NAMED - set(listing) for listing in told] == [set()]


def test_after_play_the_run_names_the_agent_each_tool_and_the_time_of_the_listing_that_lacked_it(
    listened: Listened,
) -> None:
    lacked = listened.lacked

    assert sorted(line["tool"] for line in lacked) == sorted(NOTES_TOOLS)
    assert {(line["agent"], line["time"], line["session"]) for line in lacked} == {
        (AUTHOR, lacked[0]["time"], lacked[0]["session"]),
    }


def test_the_tool_list_notifier_admits_the_gateway_alone(listened: Listened) -> None:
    opened = listened.opened

    assert opened == {"own workspace key": 401, "no key": 401, "the gateway's key": 200}
