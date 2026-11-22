"""What the Docker tests share: whether they can run, how they call Docker, and the workspace service."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Mapping

import pytest
from loc_arena.config import RunConfig
from loc_arena.episode_stack import CONFIGS_DIRECTORY
from loc_arena.stack_play import EPISODE_SERVICE

from tests.stack_slots import UNMEASURED_SECONDS

DOCKER_CHECK_TIMEOUT_SECONDS = 10
WORKSPACE_START_SECONDS = 60
LOG_TAIL_LINES = 200  # of each log a failure's report holds
LOG_READ_SECONDS = 30
PROBE_OUTPUT_CHARACTERS = 300  # of each health probe's output a failure's report holds
# Defines ``serve_agents_workspace(config, code, write)`` in the episode's container: it serves the workspace
# service on ``code`` and the tool-list notifier as a played episode does, recording with ``write``, and reads
# the services' records for notices as play does, while its result is held. The tools gateway puts both on its
# route beside the live services', so it answers no call without them.
SERVE_WORKSPACE = """
import threading, time
from loc_arena.live import build_subagent_briefs
from loc_arena.own_sessions import serve_play_tools
from loc_arena.sessions import Sessions
from loc_arena.subagents import Subagents
from loc_arena.sandbox import token_secret_name
from loc_arena.workspace_service import Turns
def serve_agents_workspace(config, code, write):
    secrets = config.settings.gateway.secrets_dir
    tokens = {a.id: (secrets / token_secret_name(a.id)).read_text().strip() for a in config.agents}
    subagents = Subagents(config, build_subagent_briefs(config), secrets)  # as play serves delegate
    sessions = Sessions(config, {}, secrets, code, {}, 30)  # no agent starts one in these tests
    serving = serve_play_tools(config, code, tokens, write, Turns(tokens), subagents, sessions)
    notices = serving.__enter__()
    def read_notices():
        while True:
            notices.read()
            time.sleep(config.settings.pi.notice_poll_seconds)
    threading.Thread(target=read_notices, daemon=True).start()
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


def read_logs(compose: list[str], environment: Mapping[str, str], services: list[str]) -> str:
    """The end of each service's log for a failure's report; it never raises, so it hides no failure."""
    reports = []
    for service in services:
        try:
            done = subprocess.run(
                ["docker", *compose, "logs", "--no-log-prefix", "--tail", str(LOG_TAIL_LINES), service],
                capture_output=True,
                text=True,
                errors="replace",
                env=environment,
                timeout=LOG_READ_SECONDS,
            )
            reports.append(f"\nlog of {service}:\n{done.stdout}{done.stderr}")
        except (OSError, subprocess.TimeoutExpired) as error:
            reports.append(f"\nlog of {service} not read: {error}")
    return "".join(reports)


def read_unhealthy(compose: list[str], environment: Mapping[str, str]) -> str:
    """For a failure's report: each container not running and healthy, its health checks and its log.

    It never raises, so it hides no failure.
    """
    listing = ["docker", *compose, "ps", "--all", "--format", "json"]
    try:
        listed = subprocess.run(
            listing,
            capture_output=True,
            text=True,
            env=environment,
            timeout=LOG_READ_SECONDS,
        )
        # One object per line, or one array: older Compose prints the second.
        parsed = [json.loads(line) for line in listed.stdout.splitlines()]
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return f"\nthe containers were not listed: {error!r}"
    if listed.returncode:
        return f"\nthe containers were not listed: {listed.stderr.strip()}"
    containers = [item for entry in parsed for item in (entry if isinstance(entry, list) else [entry])]
    well = {("running", ""), ("running", "healthy")}
    unhealthy = [entry for entry in containers if (entry.get("State"), entry.get("Health")) not in well]
    reports = [_report_container(compose, environment, container) for container in unhealthy]
    return "".join(reports) if containers else "\nno container was listed"


def _report_container(
    compose: list[str],
    environment: Mapping[str, str],
    container: dict[str, str],
) -> str:
    """One container's state, start, every health probe and the end of its log; or why it was not read."""
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container["Name"]],
            capture_output=True,
            text=True,
            timeout=LOG_READ_SECONDS,
        )
        state = json.loads(inspected.stdout)
        health = state.get("Health") or {}
        probes = "".join(
            f"\n  probe {probe['Start']} to {probe['End']}: exit {probe['ExitCode']} "
            f"{probe['Output'][:PROBE_OUTPUT_CHARACTERS]!r}"
            for probe in health.get("Log", [])
        )
        report = (
            f"\n{container['Service']}: {state['Status']}, started {state['StartedAt']}, "
            f"health {health.get('Status', 'none')} after {health.get('FailingStreak', 0)} failed probes"
            f"{probes}"
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as error:
        report = f"\n{container.get('Name', container)} not read: {error!r}"
    return report + read_logs(compose, environment, [container.get("Service", "")])


def needs_stack(*images: str, seconds: int = UNMEASURED_SECONDS) -> list[pytest.MarkDecorator]:
    """A Docker test module's marks: it runs in a stack slot, and skips without Docker or ``images``.

    ``seconds`` is about how long the module takes on a CI runner: it evens the shares of ``--stack-shard``.
    """
    missing = not all(map(image_exists, images))
    reason = f"needs Docker and {', '.join(images)}"
    return [pytest.mark.stack(seconds=seconds), pytest.mark.skipif(missing, reason=reason)]


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
