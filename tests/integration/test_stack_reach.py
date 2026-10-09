"""What agent code can reach in a stack run: every "no" of the plan's reach table, from where agent code runs.

The rendered compose project is brought up for real, once. Inside the episode container, the agents' code
tools run commands through the sandbox client as a played episode does, so each probe is an agent's bash call
in the sandbox. Probes are `python -c` scripts: the image has no curl, wget or nc, and a missing binary would
make a "must fail" row pass for the wrong reason. Each prints one outcome token (`resolved`, `connected`, an
errno or `gaierror:` name, a JSON list of what should not be there), and a test asserts the token. The network
rows are only meaningful beside their positive controls: the sandbox resolves and reaches the gateway.
Skipped unless the Docker daemon answers and the stack image exists; the image is rebuilt first, so it holds
this code.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import (
    AGENT_NETWORK,
    CONFIGS_DIRECTORY,
    EGRESS_NETWORK,
    OUTPUT_DIRECTORY,
    REPOSITORY,
    SCENARIOS_DIRECTORY,
    render_compose,
)
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.gateway.proxy import GatewayCall
from loc_arena.sandbox import SANDBOX_SERVICE, TOKEN_SECRET_NAME, TOKEN_VARIABLE

from tests.integration._docker_support import image_exists

pytestmark = pytest.mark.skipif(
    not image_exists(),
    reason="needs a reachable Docker daemon and the stack image",
)

RUN = "aurora-efficiency.deterministic"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
CHECKOUT_REPOS = [
    "meridian-common",
    "meridian-controlplane",
    "meridian-datapipe",
    "meridian-distill",
    "meridian-evalkit",
    "meridian-jobsvc",
    "meridian-serving",
]
NOT_ALLOWED_PATH = "not-an-allowed-path"  # a path outside settings.gateway.allowed_paths: nothing leaves
INTERNET_ADDRESS = "1.1.1.1"
INTERNET_PORT = 443
CONNECT_TIMEOUT_SECONDS = 3
# What Docker mounts in any container, and the init the sandbox runs under (`init: true`).
DOCKER_MOUNTS = ["/", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/usr/sbin/docker-init"]
KERNEL_MOUNT_ROOTS = ("/proc", "/dev", "/sys")
# Run in the episode container: a sealed log where play writes one, a checkout where play seeds one, and the
# agents' bash asked to read the one, to list the other, and to leave a process running and look for it, then
# every probe the test sends on stdin: the commands to run in the sandbox, and the key to look for here.
IN_EPISODE = f"""
import json
import os
import sys
from pathlib import Path
from loc_arena.config import load_run_config
from loc_arena.sandbox import connect_sandbox
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.tools import StubServices
from loc_arena.task import seed_episode_checkout

config = load_run_config("{CONFIGS_DIRECTORY}/{RUN}.yaml")
episode = Path("{OUTPUT_DIRECTORY}/a-run/episode")
episode.mkdir(parents=True)
sealed = episode / "events.sealed.jsonl"
sealed.write_text("sealed\\n")
services = CodeServices(
    StubServices(),
    checkout=seed_episode_checkout(config, episode),
    repos=[],
    stack=config.settings.stack,
    sandbox=connect_sandbox(config.settings),
)
bash = lambda command: services.run("bash", {{"command": command}})
results = [bash(command) for command in (f"cat {{sealed}}", "ls")]
left = bash("setsid sleep 300 > /dev/null 2>&1 < /dev/null & echo $!")
results.append(bash(f"kill -0 {{left['stdout'].strip()}}"))
sent = json.load(sys.stdin)
secrets_dir = config.settings.gateway.secrets_dir
holds_key = {API_KEY_VARIABLE!r} in os.environ or any(sent["key"] in value for value in os.environ.values())
print(json.dumps({{
    "bash": results,
    "probes": {{name: bash(command) for name, command in sent["probes"].items()}},
    "episode_environment_key": "present" if holds_key else "absent",
    "episode_secrets": sorted(path.name for path in secrets_dir.iterdir()),
}}))
"""
# Defined first in every probe: what exists, connections, names, environments, mounts and routes.
PRELUDE = f"""
import errno, glob, json, os, socket
getaddrinfo_error_names = {{code: name for name, code in vars(socket).items() if name.startswith("EAI_")}}
def list_paths(pattern):
    return json.dumps(sorted(glob.glob(pattern)))
def attempt(host, port):
    try:
        socket.create_connection((host, port), {CONNECT_TIMEOUT_SECONDS}).close()
    except socket.gaierror as error:
        return "gaierror:" + getaddrinfo_error_names[error.args[0]]
    except TimeoutError:
        return "timeout"
    except OSError as error:
        return errno.errorcode[error.errno]
    return "connected"
def resolve(name):
    try:
        socket.gethostbyname(name)
    except socket.gaierror as error:
        return "gaierror:" + getaddrinfo_error_names[error.args[0]]
    return "resolved"
def search_environments(needle):
    environments = []
    for path in glob.glob("/proc/[0-9]*/environ"):
        try:
            environments.append(open(path, "rb").read())
        except OSError:
            pass
    if any(needle in environment for environment in environments):
        return "found"
    return "absent" if any(environments) else "unreadable"
def list_mounts():
    mounts = {{}}
    for line in open("/proc/self/mountinfo"):
        fields = line.split()
        if not any(fields[4] == root or fields[4].startswith(root + "/") for root in {KERNEL_MOUNT_ROOTS!r}):
            mounts[fields[4]] = fields[3]
    return mounts
def list_routes():
    routes = []
    for line in open("/proc/net/route").read().splitlines()[1:]:
        fields = line.split()
        destination = socket.inet_ntoa(int(fields[1], 16).to_bytes(4, "little"))
        routes.append("%s/%d" % (destination, bin(int(fields[7], 16)).count("1")))
    return routes
"""
REFUSED_PATH_SCRIPT = """
import urllib.error, urllib.request
request = urllib.request.Request('http://gateway:{gateway_port}/{not_allowed_path}', b'')
try:
    urllib.request.urlopen(request)
except urllib.error.HTTPError as error:
    print(error.code)
"""
# Each row of the reach table: the test's name, its probe (formatted with the values the fixture finds) and
# the outcomes the table allows. A listener waits on every address of this host, so a refusal is a firewall's.
ROWS: dict[str, tuple[str, set[str]]] = {
    "the_sandbox_resolves_the_gateway": ("print(resolve('gateway'))", {"resolved"}),
    "the_sandbox_connects_to_the_gateway": ("print(attempt('gateway', {gateway_port}))", {"connected"}),
    "no_process_environment_holds_the_key": ("print(search_environments({key}))", {"absent"}),
    "the_sandbox_holds_only_its_token": (
        "print(json.dumps(sorted(os.listdir('{secrets_dir}'))))",
        {json.dumps([TOKEN_SECRET_NAME])},
    ),
    "the_sandbox_mounts_only_its_checkouts": (
        "print(json.dumps(sorted(set(list_mounts()) - set({allowed_mounts}))))",
        {"[]"},
    ),
    "the_sandbox_checkouts_are_the_projects_volume": (
        "print(list_mounts()['{checkouts}'].endswith('/{checkouts_volume}/_data'))",
        {"True"},
    ),
    "the_sandbox_routes_only_to_agent_net": (
        "print(json.dumps(sorted(set(list_routes()) - set(['{agent_subnet}']))))",
        {"[]"},
    ),
    "the_sandbox_cannot_connect_to_the_internet": (
        "print(attempt('{internet_address}', {internet_port}))",
        {"ENETUNREACH"},
    ),
    "the_sandbox_cannot_resolve_the_providers_name": (
        "print(resolve('{provider_host}'))",
        {"gaierror:EAI_AGAIN"},
    ),
    "the_sandbox_cannot_resolve_any_outside_name": (
        "print(resolve('{outside_name}'))",
        {"gaierror:EAI_AGAIN"},
    ),
    "the_sandbox_cannot_reach_the_host_by_name": (
        "print(attempt('host.docker.internal', {host_port}))",
        {"gaierror:EAI_AGAIN"},
    ),
    "the_sandbox_cannot_reach_the_host_by_address": (
        "print(attempt('{host_address}', {host_port}))",
        {"ENETUNREACH"},
    ),
    "the_sandbox_holds_no_reference_answer": ("print(list_paths('{scenarios}/*/reference'))", {"[]"}),
    "the_sandbox_holds_no_scripted_move": ("print(list_paths('{scenarios}/*/scripted'))", {"[]"}),
    "the_gateway_refuses_an_unlisted_path": (REFUSED_PATH_SCRIPT, {str(int(HTTPStatus.FORBIDDEN))}),
}


@dataclass(frozen=True)
class StackRun:
    """What one bring-up of the stack produced: the commands' results, the episode's view, the call log."""

    bash: list[dict[str, Any]]
    probes: dict[str, dict[str, Any]]
    episode_environment_key: str
    episode_secrets: list[str]
    calls: list[GatewayCall]
    project: str
    agent_net_gateway: str | None

    def read_outcome(self, row: str) -> str:
        """The one token the probe of ``row`` printed in the sandbox."""
        return self.probes[row]["stdout"].strip()


@dataclass(frozen=True)
class NetworkAddressing:
    """A compose network's subnet, and the host's address on it: none when the network is isolated."""

    subnet: str
    gateway: str | None


def build_probes(
    key: str,
    project: str,
    agent_subnet: str,
    host_address: str,
    host_port: int,
) -> dict[str, str]:
    """The sandbox command of each row of ``ROWS``, by the row's name."""
    gateway, stack = CONFIG.settings.gateway, CONFIG.settings.stack
    values = {
        "secrets_dir": gateway.secrets_dir,
        "gateway_port": gateway.port,
        "not_allowed_path": NOT_ALLOWED_PATH,
        "key": repr(key.encode()),
        "allowed_mounts": json.dumps([*DOCKER_MOUNTS, str(stack.checkouts_directory)]),
        "agent_subnet": agent_subnet,
        "internet_address": INTERNET_ADDRESS,
        "internet_port": INTERNET_PORT,
        "provider_host": urlsplit(str(gateway.upstream)).hostname,
        "outside_name": f"{secrets.token_hex(8)}.example.com",
        "host_address": host_address,
        "host_port": host_port,
        "scenarios": SCENARIOS_DIRECTORY,
        "checkouts": stack.checkouts_directory,
        "checkouts_volume": f"{project}_checkouts",
    }
    return {
        row: f"python -c {shlex.quote(PRELUDE + script.format(**values))}"
        for row, (script, _) in ROWS.items()
    }


def find_host_address(read_in_gateway: Callable[[str], str], egress_gateway: str | None) -> str:
    """This host's address as a container with a route out reaches it.

    On Docker Desktop that is host.docker.internal, which forwards to the Mac; on Linux, where that name does
    not resolve, it is the host's address on egress-net.
    """
    address = read_in_gateway("host.docker.internal") or egress_gateway
    if address is None:
        raise RuntimeError(
            "found no address of this host: host.docker.internal and egress-net's gateway are missing",
        )
    return address


def read_addressing(network: str) -> NetworkAddressing:
    """The subnet and gateway address of the Docker network named ``network``."""
    inspected = run_docker("network", "inspect", network, "--format", "{{json .IPAM.Config}}")
    addressing = json.loads(inspected.stdout)[0]
    return NetworkAddressing(addressing["Subnet"], addressing.get("Gateway"))


def run_docker(*arguments: str, **options: Any) -> subprocess.CompletedProcess[str]:
    """Run ``docker`` with ``arguments``; raise with its stderr when it fails."""
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True, check=False, **options)
    if done.returncode:
        raise RuntimeError(f"docker {shlex.join(arguments[:6])} exited {done.returncode}: {done.stderr}")
    return done


@pytest.fixture(scope="module")
def stack_run(tmp_path_factory: pytest.TempPathFactory) -> StackRun:
    directory = tmp_path_factory.mktemp("stack")
    compose_file = directory / "compose.yaml"
    gateway = CONFIG.settings.gateway
    compose_file.write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    project = f"locarena-test-{secrets.token_hex(3)}"
    # The project directory holds no .env, so the gateway gets this random placeholder and never the real key:
    # a match of it anywhere is unambiguous.
    placeholder = secrets.token_urlsafe(24)
    compose = ["compose", "-p", project, "-f", str(compose_file), "--project-directory", str(directory)]
    environment = {
        **os.environ,
        API_KEY_VARIABLE: placeholder,
        TOKEN_VARIABLE: secrets.token_urlsafe(32),
    }
    call_log = directory / "calls.jsonl"
    try:
        up = [*compose, "up", "--detach", "--wait", "--build", "gateway", SANDBOX_SERVICE]
        run_docker(*up, env=environment)
        agent_net, egress_net = (
            read_addressing(f"{project}_{network}") for network in (AGENT_NETWORK, EGRESS_NETWORK)
        )
        resolve = "import socket, sys\ntry: print(socket.gethostbyname(sys.argv[1]))\nexcept OSError: pass"
        in_gateway = [*compose, "exec", "-T", "gateway", "python", "-c", resolve]
        host_address = find_host_address(
            lambda name: run_docker(*in_gateway, name, env=environment).stdout.strip(),
            egress_net.gateway,
        )
        with socket.create_server(("0.0.0.0", 0)) as listener:  # noqa: S104 - every address of this host, on purpose
            probes = build_probes(
                placeholder,
                project,
                agent_net.subnet,
                host_address,
                listener.getsockname()[1],
            )
            played = run_docker(
                *compose,
                "run",
                "--rm",
                "-T",
                "episode",
                "python",
                "-c",
                IN_EPISODE,
                input=json.dumps({"key": placeholder, "probes": probes}),
                env=environment,
            )
        run_docker(*compose, "cp", f"gateway:{gateway.call_log}", str(call_log), env=environment)
    finally:
        down = ["docker", *compose, "down", "--volumes", "--remove-orphans"]
        subprocess.run(down, env=environment, capture_output=True, check=False)
    printed = json.loads(played.stdout.strip().splitlines()[-1])
    calls = [GatewayCall.model_validate_json(line) for line in call_log.read_text().splitlines()]
    return StackRun(calls=calls, project=project, agent_net_gateway=agent_net.gateway, **printed)


def test_bash_in_a_stack_run_cannot_read_the_episodes_sealed_log(stack_run: StackRun) -> None:
    read = stack_run.bash[0]

    missing = "No such file or directory" in read["stderr"]
    assert (read["returncode"], read["stdout"], missing) == (1, "", True)


def test_bash_in_a_stack_run_sees_the_seeded_checkout(stack_run: StackRun) -> None:
    listed = stack_run.bash[1]

    expected = "".join(f"{repo}\n" for repo in CHECKOUT_REPOS)
    assert listed == {"returncode": 0, "stdout": expected, "stderr": ""}


def test_a_process_a_bash_command_leaves_running_ends_with_it(stack_run: StackRun) -> None:
    looked_for = stack_run.bash[2]

    gone = "No such process" in looked_for["stderr"]
    assert (looked_for["returncode"], gone) == (1, True)


@pytest.mark.parametrize(("row", "allowed"), [(row, allowed) for row, (_, allowed) in ROWS.items()], ids=ROWS)
def test_sandbox_probe_prints_an_outcome_the_reach_table_allows(
    stack_run: StackRun,
    row: str,
    allowed: set[str],
) -> None:
    outcome = stack_run.read_outcome(row)

    assert outcome in allowed


def test_the_gateway_records_the_sandbox_as_the_caller_of_a_refused_path(stack_run: StackRun) -> None:
    sandbox_caller = f"{stack_run.project}-{SANDBOX_SERVICE}-"

    refused = [call for call in stack_run.calls if call.path == f"/{NOT_ALLOWED_PATH}"]

    assert [(call.status, call.caller.startswith(sandbox_caller)) for call in refused] == [
        (HTTPStatus.FORBIDDEN, True),
    ]


def test_the_host_holds_no_address_on_agent_net(stack_run: StackRun) -> None:
    held = stack_run.agent_net_gateway

    assert held is None


def test_the_episode_container_holds_no_key(stack_run: StackRun) -> None:
    held = stack_run.episode_environment_key

    assert held == "absent"


def test_the_episode_container_holds_only_the_sandbox_token(stack_run: StackRun) -> None:
    held = stack_run.episode_secrets

    assert held == [TOKEN_SECRET_NAME]
