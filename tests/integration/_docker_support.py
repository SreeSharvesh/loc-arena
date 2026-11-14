"""What the Docker tests share: whether they can run, how they call Docker, and the workspace service."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Mapping

from loc_arena.config import RunConfig
from loc_arena.episode_stack import CONFIGS_DIRECTORY
from loc_arena.stack_play import EPISODE_SERVICE

DOCKER_CHECK_TIMEOUT_SECONDS = 10
WORKSPACE_START_SECONDS = 60
# Defines ``serve_agents_workspace(config, code, write)`` in the episode's container: it serves the workspace
# service on ``code`` as a played episode does, recording with ``write``, while its result is held. The tools
# gateway puts that service on its route beside the live services', so it answers no call without it.
SERVE_WORKSPACE = f"""
from loc_arena.notices import Notices, add_notice_tools
from loc_arena.sandbox import token_secret_name
from loc_arena.workspace_service import Turns, build_workspace_server, serve_workspace
def serve_agents_workspace(config, code, write):
    secrets = config.settings.gateway.secrets_dir
    tokens = {{a.id: (secrets / token_secret_name(a.id)).read_text().strip() for a in config.agents}}
    server = build_workspace_server(code, tokens, write, Turns(tokens))
    add_notice_tools(server, Notices(config, tokens))
    serving = serve_workspace(server, {EPISODE_SERVICE!r}, config.settings.stack.workspace_port)
    serving.__enter__()
    return serving  # held by the caller: it stops serving once nothing holds it
"""
# The episode's container serving the workspace service alone, over no checkout, until it is removed.
_SERVE_WORKSPACE_ALONE = f"""
import sys, threading
from loc_arena.config import load_run_config
from loc_arena.scaffold.code_services import CodeServices
{SERVE_WORKSPACE}
config = load_run_config(sys.argv[1])
code = CodeServices(checkouts={{}}, repos=[], stack=config.settings.stack)
serving = serve_agents_workspace(config, code, lambda line: None)
threading.Event().wait()
"""


def read_mounts(project: str) -> dict[str, list[str]]:
    """The source and destination of every mount of each container of ``project``, by compose service."""
    label = "com.docker.compose.project"
    listed = run_docker("ps", "--all", "--quiet", "--filter", f"label={label}={project}").stdout.split()
    layout = '{{index .Config.Labels "com.docker.compose.service"}} {{json .Mounts}}'
    mounts: dict[str, list[str]] = {}
    for line in run_docker("inspect", "--format", layout, *listed).stdout.splitlines():
        service, _, inspected = line.partition(" ")
        mounts.setdefault(service, []).extend(
            path
            for mount in json.loads(inspected)
            for path in (mount.get("Source", ""), mount["Destination"])
        )
    return mounts


def run_docker(
    *arguments: str,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker`` with ``arguments``, ``env`` and ``input``; raise with its stderr when it fails."""
    done = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        input=input,
    )
    if done.returncode:
        raise RuntimeError(f"docker {shlex.join(arguments[:6])} exited {done.returncode}: {done.stderr}")
    return done


def read_sandbox_logs(
    compose: list[str],
    environment: Mapping[str, str],
    sandboxes: list[str],
    failure: str,
) -> str:
    """The log of each sandbox ``failure`` names, or of every sandbox when it names none, for its report."""
    named = [sandbox for sandbox in sandboxes if sandbox in failure] or sandboxes
    logs = ["docker", *compose, "logs", "--no-log-prefix"]
    read = [subprocess.run([*logs, name], capture_output=True, text=True, env=environment) for name in named]
    return "".join(
        f"\nlog of {name}:\n{done.stdout}{done.stderr}" for name, done in zip(named, read, strict=True)
    )


def image_exists(image: str) -> bool:
    """Whether the Docker daemon answers and holds ``image``."""
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=DOCKER_CHECK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return inspected.returncode == 0


def serve_workspace(compose: list[str], environment: Mapping[str, str], config: RunConfig, run: str) -> None:
    """Start the episode's container of the project ``compose`` names, serving the workspace service alone.

    Returns once it listens; ``docker compose down`` removes it with the project.
    """
    script = ["python", "-c", _SERVE_WORKSPACE_ALONE, f"{CONFIGS_DIRECTORY}/{run}.yaml"]
    started = subprocess.run(
        [*compose, "run", "--detach", "--no-deps", "--use-aliases", "-T", EPISODE_SERVICE, *script],
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr[-2000:]
    container = started.stdout.strip().splitlines()[-1]
    port = config.settings.stack.workspace_port
    probe = f"import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()"
    deadline = time.monotonic() + WORKSPACE_START_SECONDS
    while subprocess.run(
        ["docker", "exec", container, "python", "-c", probe],
        capture_output=True,
    ).returncode:
        assert time.monotonic() < deadline, f"the workspace service did not start in {container}"
        time.sleep(0.5)
