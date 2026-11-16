"""A long bash call through the tools gateway, in real containers: the gateway waits as long as the sandbox.

Brings up the rendered compose project's tools gateway, the services on its route and serving-agent's sandbox,
and serves the workspace service with serving-agent's checkout there (the images are built first, so they
hold this code). From inside IAM's container, as agent-net is isolated, serving-agent calls bash through the
gateway with its sandbox token as pi does: the 2025 protocol, a session. Its command outlasts agentgateway's
fallback limit of 10 s on an MCP upstream call. Skipped unless the Docker daemon answers and the stack image
exists.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config, sandbox_service
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose, token_variable
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE

from tests.integration._docker_support import needs_stack, serve_workspace

RUN = "aurora-efficiency.stack-test"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
AGENT = "serving-agent"
TIMEOUT = 900
COMMAND = "sleep 15 && echo finished"  # longer than agentgateway's 10 s fallback
TOOLS_URL = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
# In IAM's container: the structured result of one bash call as pi makes it, the sandbox token on stdin.
CALL_BASH = """
import anyio, httpx2, json, sys
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
async def call(url, token, command):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120) as http,
        Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
    ):
        result = await client.call_tool("bash", {"command": command})
    if result.is_error or result.structured_content is None:
        return {"error": " ".join(block.text for block in result.content if isinstance(block, TextContent))}
    return dict(result.structured_content)
print(json.dumps(anyio.run(call, sys.argv[1], sys.stdin.read(), sys.argv[2])))
"""

pytestmark = needs_stack(CONFIG.settings.stack.image)


@pytest.fixture(scope="module")
def called(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    compose_file = tmp_path_factory.mktemp("tools-gateway") / "compose.yaml"
    compose_file.write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    command = ["docker", "compose", "-p", f"locarena-tools-{secrets.token_hex(3)}", "-f", str(compose_file)]
    command += ["--project-directory", str(REPOSITORY)]
    attack, _ = issue_secrets(CONFIG)
    environment = {**os.environ, **attack, "OPENROUTER_API_KEY": "no-calls-dummy"}

    def compose(*arguments: str, stdin: str | None = None) -> str:
        done = subprocess.run(
            [*command, *arguments],
            input=stdin,
            env=environment,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        assert done.returncode == 0, done.stderr[-3000:]
        return done.stdout

    try:
        compose("build")
        compose("up", "--detach", "--wait", TOOLS_GATEWAY_SERVICE, sandbox_service(AGENT))
        serve_workspace(command, environment, CONFIG, RUN, [AGENT])
        call = ["exec", "-T", "iam", "python", "-c", CALL_BASH, TOOLS_URL, COMMAND]
        return json.loads(compose(*call, stdin=attack[token_variable(AGENT)]).strip().splitlines()[-1])
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            env=environment,
            capture_output=True,
        )


def test_a_bash_call_longer_than_the_gateways_fallback_limit_returns_its_result(
    called: dict[str, Any],
) -> None:
    result = called

    assert result == {"returncode": 0, "stdout": "finished\n", "stderr": ""}
