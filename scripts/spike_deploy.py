"""Spike, throwaway: redeploy a company service from the forge's merged main, in the stack the tests start.

``python scripts/spike_deploy.py <state directory> <command> ...``. ``up`` renders the stack-test compose file
with three additions and starts it: a ``releases`` volume every engine-module service mounts read-only, a
``/releases/current/<repo>`` entry ahead of each company repo on those services' PYTHONPATH, and a ``deployer``
container that alone writes the volume. ``deploy`` archives a repo's main in the forge's container, unpacks it
as a new release, swaps ``current`` and restarts the services named. No engine or company code changes.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose, token_variable
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE
from meridian_forge.repositories import repository_path

from tests.integration._docker_support import serve_workspace
from tests.integration._mcp_probe import CALL_TOOL

RUN = "aurora-efficiency.stack-test"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
RELEASES = "/releases"
TOOL_CALL = (
    CALL_TOOL
    + """
url, tool, arguments = sys.argv[1:4]
print(json.dumps(call_tool(url, sys.stdin.read(), tool, json.loads(arguments))))
"""
)
# One MCP session held open through the tools gateway, calling a tool twice a second: does it survive a restart?
PROBE = """
import anyio, httpx2, json, sys, time
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
async def main(url, token, tool, arguments, seconds):
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=10) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            at = time.time()
            try:
                result = await client.call_tool(tool, arguments)
                text = " ".join(b.text for b in result.content if isinstance(b, TextContent))
                said = ("ERROR " if result.is_error else "ok ") + text[:160]
            except Exception as error:
                said = "EXCEPTION " + repr(error)[:200]
            print(json.dumps([round(at, 2), round(time.time() - at, 2), said]), flush=True)
            await anyio.sleep(0.5)
url, tool, arguments, seconds = sys.argv[1:5]
anyio.run(main, url, sys.stdin.read(), tool, json.loads(arguments), float(seconds))
"""
UNPACK = """set -eu
new=/releases/$1
rm -rf "$new" && mkdir -p "$new"
if [ -e /releases/current ]; then cp -a /releases/current/. "$new"/; fi
rm -rf "$new/$2" && mkdir "$new/$2" && tar -x -C "$new/$2"
ln -sfn "$new" /releases/next && mv -T /releases/next /releases/current
"""


class Spike:
    def __init__(self, state: Path) -> None:
        self.state = state
        saved = json.loads((state / "state.json").read_text())
        self.compose: list[str] = saved["compose"]
        self.play: dict[str, str] = saved["play"]
        self.env = {**os.environ, **self.play}

    def run(self, *arguments: str, stdin: str | bytes | None = None, check: bool = True, text: bool = True):
        done = subprocess.run(
            [*self.compose, *arguments],
            env=self.env,
            input=stdin,
            capture_output=True,
            text=text,
            check=False,
        )
        if check and done.returncode != 0:
            sys.exit(f"{' '.join(arguments[:5])} failed: {done.stderr[-3000:]}")
        return done

    def tool(self, agent: str, tool: str, arguments: dict) -> dict:
        url = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
        token = self.play[token_variable(agent)]
        done = self.run(
            "exec", "-T", "iam", "python", "-c", TOOL_CALL, url, tool, json.dumps(arguments), stdin=token
        )
        return json.loads(done.stdout.strip().splitlines()[-1])

    def wait_healthy(self, *services: str) -> float:
        started = time.monotonic()
        done = self.run("up", "--detach", "--wait", "--no-recreate", "--no-deps", *services, check=False)
        return (time.monotonic() - started) if done.returncode == 0 else -1.0

    def release(self, repo: str) -> str:
        """Archive ``repo``'s main in the forge's container into a new release, and make it current."""
        git = ["git", "--git-dir", str(repository_path(CONFIG.settings.forge.repositories, repo))]
        commit = self.run("exec", "-T", "forge", *git, "rev-parse", "main").stdout.strip()
        archive = self.run("exec", "-T", "forge", *git, "archive", "--format=tar", "main", text=False).stdout
        name = f"{repo}-{commit[:12]}"
        self.run("exec", "-T", "deployer", "sh", "-c", UNPACK, "sh", name, repo, stdin=archive, text=False)
        return name


def patch(document: dict) -> dict:
    """The stack's compose document, with the release volume on every service that serves company code."""
    document["volumes"]["releases"] = {}
    for service in CONFIG.live_services:
        entry = document["services"][service.name]
        pythonpath = entry.get("environment", {}).get("PYTHONPATH")
        if not pythonpath:
            continue  # a ready image, such as Phoenix
        released = [f"{RELEASES}/current/{Path(path).name}" for path in pythonpath.split(":")]
        entry["environment"] = {**entry["environment"], "PYTHONPATH": ":".join([*released, pythonpath])}
        entry["volumes"] = [*entry.get("volumes", []), f"releases:{RELEASES}:ro"]
        entry["restart"] = "unless-stopped"
    document["services"]["deployer"] = {
        "image": CONFIG.settings.stack.image,
        "user": "0",
        "command": ["sleep", "infinity"],
        "volumes": [f"releases:{RELEASES}"],
        "network_mode": "none",
        "init": True,
    }
    return document


def up(state: Path, services: list[str]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "compose.yaml").write_text(yaml.safe_dump(patch(render_compose(CONFIG, REPOSITORY, RUN, []))))
    attack, _ = issue_secrets(CONFIG)
    project = f"locarena-deployspike{secrets.token_hex(3)}"
    compose = ["docker", "compose", "-p", project, "-f", str(state / "compose.yaml")]
    compose += ["--project-directory", str(state)]
    play = {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}
    (state / "state.json").write_text(json.dumps({"compose": compose, "play": play}))
    spike = Spike(state)
    started = time.monotonic()
    spike.run("up", "--detach", "--wait", "--no-build", "deployer", *services)
    print(f"up {services}: {time.monotonic() - started:.1f}s")
    serve_workspace(spike.compose, spike.env, CONFIG, RUN)
    started = time.monotonic()
    spike.run("up", "--detach", "--wait", "--no-build", TOOLS_GATEWAY_SERVICE)
    print(f"up tools gateway and every tool service: {time.monotonic() - started:.1f}s")


def main() -> None:
    state, command, *rest = Path(sys.argv[1]), sys.argv[2], *sys.argv[3:]
    if command == "up":
        return up(state, rest)
    spike = Spike(state)
    if command == "down":
        spike.run("down", "--volumes", "--remove-orphans")
    elif command == "compose":  # any compose command, its output passed through
        done = spike.run(*rest, check=False)
        print(done.stdout, end="")
        print(done.stderr[-3000:], end="", file=sys.stderr)
        sys.exit(done.returncode)
    elif command == "sh":  # sh <service> <script>, the script's stdin from ours
        done = spike.run("exec", "-T", rest[0], "bash", "-c", rest[1], check=False)
        print(done.stdout, end="")
        print(done.stderr[-3000:], end="", file=sys.stderr)
        sys.exit(done.returncode)
    elif command == "tool":  # tool <agent> <tool> <json arguments>
        print(json.dumps(spike.tool(rest[0], rest[1], json.loads(rest[2]))))
    elif command == "release":  # release <repo>
        started = time.monotonic()
        print(f"released {spike.release(rest[0])} in {time.monotonic() - started:.2f}s")
    elif command == "restart":  # restart <service>...: compose's restart, then wait for health
        started = time.monotonic()
        spike.run("restart", *rest)
        healthy = spike.wait_healthy(*rest)
        print(f"restart {rest}: {time.monotonic() - started:.1f}s to healthy (wait {healthy:.1f}s)")
    elif command == "probe":  # probe <agent> <tool> <json arguments> <seconds>
        url = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
        token = spike.play[token_variable(rest[0])]
        done = spike.run(
            "exec",
            "-T",
            "iam",
            "python",
            "-c",
            PROBE,
            url,
            rest[1],
            rest[2],
            rest[3],
            stdin=token,
            check=False,
        )
        print(done.stdout, end="")
        print(done.stderr[-2000:], end="", file=sys.stderr)
    else:
        sys.exit(f"unknown command {command}")


if __name__ == "__main__":
    main()
