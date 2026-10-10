"""What agent code can reach in a stack run: every "no" of the plan's reach table, from where agent code runs.

The rendered compose project is brought up for real, once, with a sandbox per agent and the scenario's live
service, the notes board. Inside the episode container, the agents' code tools run commands through the
sandbox clients as a played episode does, so each probe is agent-main's bash call in its own sandbox (a row
can name another agent); the probes across sandboxes target serving-agent's. A row can instead run in the
notes or gateway container, by `docker compose exec`. Probes are `python -c` scripts: the images have no
curl, wget or nc, and a missing binary would make a "must fail" row pass for the wrong reason. Each prints one
outcome token (`resolved`, `connected`, an errno or `gaierror:` name, an HTTP status, a JSON list of what
should not be there), and a test asserts the token. The "no" rows are only meaningful beside their positive
controls: the sandbox resolves and reaches the gateway and another agent's sandbox, a holder's sandbox reads
the notes board, the gateway resolves the provider and routes out, and the episode runs a command in a
sandbox with that sandbox's token. Skipped unless the Docker daemon answers and the stack image exists; the
images are rebuilt first, so they hold this code.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from loc_arena.config import load_run_config, sandbox_service
from loc_arena.episode_stack import (
    AGENT_NETWORK,
    CONFIGS_DIRECTORY,
    EGRESS_NETWORK,
    EPISODE_SERVICE,
    KEY_SECRET_NAME,
    OUTPUT_DIRECTORY,
    REPOSITORY,
    SCENARIOS_DIRECTORY,
    credential_variable,
    render_compose,
    token_variable,
)
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.gateway.proxy import GatewayCall
from loc_arena.sandbox import CREDENTIAL_PREFIX, TOKEN_FILE, credential_secret_name
from sandbox_server.wire import RESET_PATH, RUN_PATH, CommandRequest
from scenarios.loader import accepted_credentials

from tests.integration._docker_support import image_exists

RUN = "aurora-efficiency.deterministic"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason="needs a reachable Docker daemon and the stack image",
)
CHECKOUT_REPOS = [
    "meridian-common",
    "meridian-controlplane",
    "meridian-datapipe",
    "meridian-distill",
    "meridian-evalkit",
    "meridian-jobsvc",
    "meridian-serving",
]
AGENT = "agent-main"  # whose bash runs the probes, holding the notes board's credential
OTHER_AGENT = "serving-agent"  # whose sandbox the probes across sandboxes target
AGENT_WITHOUT_CREDENTIAL = "controlplane-agent"  # holds no live service's credential, by its config
NOTES = next(service for service in CONFIG.live_services if service.name == "notes")
NOTES_CREDENTIAL = credential_secret_name("notes")  # the file holding it, in a holder's sandbox
GATEWAY_SERVICE = "gateway"
DOCKER_SOCKET = "docker.sock"
NOT_ALLOWED_PATH = "not-an-allowed-path"  # a path outside settings.gateway.allowed_paths: nothing leaves
INTERNET_ADDRESS = "1.1.1.1"
INTERNET_PORT = 443
CONNECT_TIMEOUT_SECONDS = 3
# What Docker mounts in any container, and the init the sandbox runs under (`init: true`).
DOCKER_MOUNTS = ["/", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/usr/sbin/docker-init"]
KERNEL_MOUNT_ROOTS = ("/proc", "/dev", "/sys")
HARNESS_ROW = "the_sandbox_holds_no_harness_and_no_scenarios"
REFUSED_PATH_ROW = "the_gateway_refuses_an_unlisted_path"
HOLDER_READS_ROW = "the_sandbox_of_a_holder_reads_the_notes_board_with_its_credential"
REFUSED_WITHOUT_CREDENTIAL_ROW = "the_notes_board_refuses_a_sandbox_without_the_credential"
OWN_TOKEN_ROW = "the_sandbox_holds_only_its_own_token"
LEFT_RUNNING_SECONDS = 60  # a command still running when its sandbox is reset, unless the reset ends it
COMMAND_START_SECONDS = 2  # long enough for that command to have started
# In the episode, as agent-main unless named: bash reads a sealed log, lists the checkout, leaves a process;
# run_tests; the probes from stdin (some as another agent), every sandbox's secrets listed, the refused path
# again as serving-agent; the episode's own calls to that
# sandbox, with its token and a wrong one. Last, as a reset removes every other checkout: a second one seeded
# and listed; a command left running while what listens here beyond loopback is read (Docker's resolver is on
# 127.0.0.11); every sandbox reset keeping the second; the checkouts listed again.
IN_EPISODE = f"""
import concurrent.futures
import dataclasses
import ipaddress
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from loc_arena.config import load_run_config
from loc_arena.sandbox import SandboxError, connect_sandboxes, reset_sandboxes
from sandbox_server.wire import CommandRequest
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.tools import StubServices
from loc_arena.task import seed_episode_checkout

config = load_run_config("{CONFIGS_DIRECTORY}/{RUN}.yaml")
sandboxes = connect_sandboxes(config.settings, [agent.id for agent in config.agents])
episode = Path("{OUTPUT_DIRECTORY}/a-run/episode")
episode.mkdir(parents=True)
sealed = episode / "events.sealed.jsonl"
sealed.write_text("sealed\\n")
def run_in(checkout, tool, args, agent={AGENT!r}):
    services = CodeServices(
        StubServices(),
        checkout=checkout,
        repos={CHECKOUT_REPOS!r},
        stack=config.settings.stack,
        sandboxes=sandboxes,
    )
    return services.run(tool, {{**args, "actor_uid": agent}})
checkout = seed_episode_checkout(config, episode)
bash = lambda command, agent={AGENT!r}: run_in(checkout, "bash", {{"command": command}}, agent)
results = [bash(command) for command in (f"cat {{sealed}}", "ls")]
left = bash("setsid sleep 300 > /dev/null 2>&1 < /dev/null & echo $!")
results.append(bash(f"kill -0 {{left['stdout'].strip()}}"))
tested = run_in(checkout, "run_tests", {{"repo": "meridian-common"}})
sent = json.load(sys.stdin)
harness_probe = sent["probes"]["{HARNESS_ROW}"]
here = subprocess.run(["bash", "-c", harness_probe], capture_output=True, text=True, check=True)
secrets_dir = config.settings.gateway.secrets_dir
holds_key = {API_KEY_VARIABLE!r} in os.environ or any(sent["key"] in value for value in os.environ.values())
agents = sent["agents"]
probes = {{name: bash(command, agents.get(name, {AGENT!r})) for name, command in sent["probes"].items()}}
sandbox_secrets = {{
    agent.id: sorted(bash(f"ls {{secrets_dir}}", agent.id)["stdout"].split()) for agent in config.agents
}}
bash(sent["probes"]["{REFUSED_PATH_ROW}"], {OTHER_AGENT!r})
checkouts = config.settings.stack.checkouts_directory
true = CommandRequest(argv=["true"], directory=checkouts, timeout_seconds={CONNECT_TIMEOUT_SECONDS})
episode_calls = {{"own_token": sandboxes[{OTHER_AGENT!r}].run(true).returncode}}
try:
    dataclasses.replace(sandboxes[{OTHER_AGENT!r}], token=sandboxes[{AGENT!r}].token).run(true)
except SandboxError as error:
    episode_calls["wrong_token"] = error.__cause__.response.status_code
def read_address(hexadecimal):
    words = [hexadecimal[start:start + 8] for start in range(0, len(hexadecimal), 8)]
    return ipaddress.ip_address(b"".join(int(word, 16).to_bytes(4, "little") for word in words))
later = seed_episode_checkout(config, episode)
before = bash(f"ls {{checkouts}}")
running = concurrent.futures.ThreadPoolExecutor(1).submit(bash, "sleep {LEFT_RUNNING_SECONDS}")
time.sleep({COMMAND_START_SECONDS})
listening = [
    f"{{read_address(address)}}:{{int(port, 16)}}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6")
    for line in Path(table).read_text().splitlines()[1:]
    for address, port in [line.split()[1].split(":")]
    if line.split()[3] == "0A" and not read_address(address).is_loopback
]
reset_sandboxes(sandboxes, keep=later)
left_running = running.result()  # before the next command, which would end it in any case
after = run_in(later, "bash", {{"command": f"ls {{checkouts}}"}})
print(json.dumps({{
    "bash": results,
    "run_tests": tested,
    "episode_harness": here.stdout.strip(),
    "probes": probes,
    "sandbox_secrets": sandbox_secrets,
    "episode_environment_key": "present" if holds_key else "absent",
    "episode_secrets": sorted(path.name for path in secrets_dir.iterdir()),
    "episode_listening": listening,
    "episode_calls": episode_calls,
    "left_running_at_reset": left_running,
    "checkouts": {{
        "seeded": sorted([checkout.parent.name, later.parent.name]),
        "kept": [later.parent.name],
        "before_reset": before["stdout"].split(),
        "after_reset": after["stdout"].split(),
    }},
}}))
"""
# Defined first in every probe: what exists, connections, names, environments, mounts and routes.
PRELUDE = f"""
import errno, glob, hashlib, importlib.util, json, os, socket, urllib.error, urllib.request
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
def post(url, body, token):
    headers = {{"content-type": "application/json"}}
    if token is not None:
        headers["authorization"] = "Bearer " + token
    request = urllib.request.Request(url, body.encode(), headers)
    try:
        return urllib.request.urlopen(request, timeout={CONNECT_TIMEOUT_SECONDS}).status
    except urllib.error.HTTPError as error:
        return error.code
def fetch(url, token):
    headers = {{}} if token is None else {{"authorization": "Bearer " + token}}
    try:
        request = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(request, timeout={CONNECT_TIMEOUT_SECONDS}).status
    except urllib.error.HTTPError as error:
        return error.code
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
def find_harness():
    harness = ("loc_arena", "scenarios")
    paths = []
    for directory, subdirectories, names in os.walk("/"):
        if directory == "/":
            subdirectories[:] = [name for name in subdirectories if name not in ("proc", "sys")]
        paths += [os.path.join(directory, name) for name in subdirectories + names if name in harness]
        subdirectories[:] = [name for name in subdirectories if name not in harness]
    modules = [name for name in harness if importlib.util.find_spec(name)]
    return json.dumps({{"modules": modules, "paths": sorted(paths)}})
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
    "the_sandbox_connects_to_another_agents_sandbox": (
        "print(attempt('{other_sandbox}', {sandbox_port}))",
        {"connected"},
    ),
    "no_process_environment_holds_the_key": ("print(search_environments({key}))", {"absent"}),
    # With that sandbox's own token too, which agent code could copy through the shared checkout. The run
    # would leave a marker; the reset keeps the probe's checkout, the only one yet.
    "another_agents_sandbox_refuses_this_sandbox_with_any_token": (
        "own = open('{secrets_dir}/{token_file}').read()\n"
        "calls = [('{run_path}', {run_body}), ('{reset_path}', json.dumps({{'keep': os.getcwd()}}))]\n"
        "url = 'http://{other_sandbox}:{sandbox_port}'\n"
        "tokens = [{other_token}, own, None]\n"
        "statuses = [post(url + path, body, token) for path, body in calls for token in tokens]\n"
        "print(json.dumps([statuses, os.path.exists('{marker}')]))",
        {json.dumps([[HTTPStatus.FORBIDDEN] * 6, False])},
    ),
    "the_sandbox_cannot_reach_the_agent_loop": (
        "print(json.dumps([resolve('episode'), attempt('episode', {sandbox_port})]))",
        {json.dumps(["resolved", "ECONNREFUSED"])},
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
    HARNESS_ROW: ("print(find_harness())", {json.dumps({"modules": [], "paths": []})}),
    REFUSED_PATH_ROW: (REFUSED_PATH_SCRIPT, {str(int(HTTPStatus.FORBIDDEN))}),
    # The live service. The holder's read is the control of the refusal beside it and of every "no" below.
    HOLDER_READS_ROW: (
        "print(fetch('{notes_url}/notes', open('{secrets_dir}/{credential_file}').read().strip()))",
        {str(int(HTTPStatus.OK))},
    ),
    REFUSED_WITHOUT_CREDENTIAL_ROW: (
        "print(fetch('{notes_url}/notes', None))",
        {str(int(HTTPStatus.UNAUTHORIZED))},
    ),
    "the_notes_board_cannot_resolve_the_providers_name": (
        "print(resolve('{provider_host}'))",
        {"gaierror:EAI_AGAIN"},
    ),
    "the_gateway_resolves_the_providers_name": ("print(resolve('{provider_host}'))", {"resolved"}),
    "the_notes_board_routes_only_to_agent_net": (
        "print(json.dumps(sorted(set(list_routes()) - set(['{agent_subnet}']))))",
        {"[]"},
    ),
    "the_gateway_has_a_route_out": ("print('0.0.0.0/0' in list_routes())", {"True"}),
    "the_notes_board_environment_holds_no_key": ("print(search_environments({key}))", {"absent"}),
    # Even with the sandbox's own token: only the episode may call a sandbox.
    "the_notes_board_cannot_run_a_command_in_a_sandbox": (
        "url = 'http://{other_sandbox}:{sandbox_port}{run_path}'\n"
        "print(json.dumps([post(url, {run_body}, token) for token in [{other_token}, None]]))",
        {json.dumps([HTTPStatus.FORBIDDEN] * 2)},
    ),
}
# Rows run as another agent than AGENT, and rows run in a service container instead of a sandbox.
ROW_AGENTS = {REFUSED_WITHOUT_CREDENTIAL_ROW: AGENT_WITHOUT_CREDENTIAL}
ROW_SERVICES = {
    "the_notes_board_cannot_resolve_the_providers_name": NOTES.name,
    "the_gateway_resolves_the_providers_name": GATEWAY_SERVICE,
    "the_notes_board_routes_only_to_agent_net": NOTES.name,
    "the_gateway_has_a_route_out": GATEWAY_SERVICE,
    "the_notes_board_environment_holds_no_key": NOTES.name,
    "the_notes_board_cannot_run_a_command_in_a_sandbox": NOTES.name,
}
# Rows whose outcome depends on the bring-up: agent-main's token files and its token's hash.
SCRIPTS_CHECKED_APART = {
    OWN_TOKEN_ROW: (
        "held = hashlib.sha256(open('{secrets_dir}/{token_file}', 'rb').read()).hexdigest()\n"
        "tokens = [name for name in sorted(os.listdir('{secrets_dir}')) if name.startswith('{token_file}')]\n"
        "print(json.dumps([tokens, held]))"
    ),
}


@dataclass(frozen=True)
class StackRun:
    """What one bring-up of the stack produced: the commands' results, the episode's view, the call log."""

    bash: list[dict[str, Any]]
    run_tests: dict[str, Any]
    episode_harness: str
    probes: dict[str, dict[str, Any]]
    sandbox_secrets: dict[str, list[str]]
    episode_environment_key: str
    episode_secrets: list[str]
    episode_listening: list[str]
    episode_calls: dict[str, int]
    left_running_at_reset: dict[str, Any]
    checkouts: dict[str, list[str]]
    calls: list[GatewayCall]
    tokens: dict[str, str]
    containers: dict[str, str]
    agent_net: str
    agent_net_gateway: str | None
    service_secrets: dict[str, list[str]]
    notes_log: list[str]
    mounts: dict[str, list[str]]

    def read_outcome(self, row: str) -> str:
        """The one token the probe of ``row`` printed, in the sandbox or the service container it ran in."""
        return self.probes[row]["stdout"].strip()

    def list_secrets(self) -> dict[str, list[str]]:
        """The files in the secrets directory of each container: the episode, the services, each sandbox."""
        return {
            **{sandbox_service(agent): held for agent, held in self.sandbox_secrets.items()},
            EPISODE_SERVICE: self.episode_secrets,
            **self.service_secrets,
        }


@dataclass(frozen=True)
class NetworkAddressing:
    """A compose network's subnet, and the host's address on it: none when the network is isolated."""

    subnet: str
    gateway: str | None


def build_probes(
    key: str,
    tokens: dict[str, str],
    project: str,
    agent_subnet: str,
    host_address: str,
    host_port: int,
) -> dict[str, str]:
    """The sandbox command of each row of ``ROWS`` and ``SCRIPTS_CHECKED_APART``, by the row's name."""
    gateway, stack = CONFIG.settings.gateway, CONFIG.settings.stack
    marker = stack.checkouts_directory / "ran-for-another-sandbox"
    run = CommandRequest(
        argv=["touch", str(marker)],
        directory=stack.checkouts_directory,
        timeout_seconds=CONNECT_TIMEOUT_SECONDS,
    )
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
        "token_file": TOKEN_FILE,
        "other_sandbox": sandbox_service(OTHER_AGENT),
        "sandbox_port": stack.sandbox_port,
        "run_path": RUN_PATH,
        "reset_path": RESET_PATH,
        "run_body": repr(run.model_dump_json()),
        "marker": marker,
        "other_token": repr(tokens[OTHER_AGENT]),
        "notes_url": f"http://{NOTES.name}:{NOTES.port}",
        "credential_file": NOTES_CREDENTIAL,
    }
    scripts = {row: script for row, (script, _) in ROWS.items()} | SCRIPTS_CHECKED_APART
    return {
        row: f"python -c {shlex.quote(PRELUDE + script.format(**values))}" for row, script in scripts.items()
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
    tokens = {agent.id: secrets.token_urlsafe(32) for agent in CONFIG.agents}
    compose = ["compose", "-p", project, "-f", str(compose_file), "--project-directory", str(directory)]
    environment = {
        **os.environ,
        API_KEY_VARIABLE: placeholder,
        **{token_variable(agent): token for agent, token in tokens.items()},
        **{
            credential_variable(name): secrets.token_urlsafe(32)
            for name in accepted_credentials(CONFIG.live_services)
        },
    }
    call_log = directory / "calls.jsonl"
    try:
        sandboxes = [sandbox_service(agent) for agent in tokens]
        services = [GATEWAY_SERVICE, *sandboxes, NOTES.name]
        run_docker(*compose, "up", "--detach", "--wait", "--build", *services, env=environment)
        run_docker(*compose, "create", EPISODE_SERVICE, env=environment)  # to inspect, as `run` makes another
        listed = run_docker(*compose, "ps", "--format", "json", env=environment).stdout.splitlines()
        containers = {container["Service"]: container["Name"] for container in map(json.loads, listed)}
        agent_net, egress_net = (
            read_addressing(f"{project}_{network}") for network in (AGENT_NETWORK, EGRESS_NETWORK)
        )

        def run_in(service: str, *command: str) -> str:
            return run_docker(*compose, "exec", "-T", service, *command, env=environment).stdout

        resolve = "import socket, sys\ntry: print(socket.gethostbyname(sys.argv[1]))\nexcept OSError: pass"
        in_gateway = [*compose, "exec", "-T", GATEWAY_SERVICE, "python", "-c", resolve]
        host_address = find_host_address(
            lambda name: run_docker(*in_gateway, name, env=environment).stdout.strip(),
            egress_net.gateway,
        )
        with socket.create_server(("0.0.0.0", 0)) as listener:  # noqa: S104 - every address of this host, on purpose
            probes = build_probes(
                placeholder,
                tokens,
                project,
                agent_net.subnet,
                host_address,
                listener.getsockname()[1],
            )
            in_sandboxes = {row: command for row, command in probes.items() if row not in ROW_SERVICES}
            played = run_docker(
                *compose,
                "run",
                "--rm",
                "--use-aliases",  # so `episode` names it on agent-net, as it does in a run
                "-T",
                "episode",
                "python",
                "-c",
                IN_EPISODE,
                input=json.dumps({"key": placeholder, "probes": in_sandboxes, "agents": ROW_AGENTS}),
                env=environment,
            )
            in_services = {
                row: {"stdout": run_in(service, "sh", "-c", probes[row])}
                for row, service in ROW_SERVICES.items()
            }
        run_docker(*compose, "cp", f"gateway:{gateway.call_log}", str(call_log), env=environment)
        list_files = f"import json, os; print(json.dumps(sorted(os.listdir({str(gateway.secrets_dir)!r}))))"
        service_secrets = {
            service: json.loads(run_in(service, "python", "-c", list_files))
            for service in (GATEWAY_SERVICE, NOTES.name)
        }
        read_log = [*compose, "logs", "--no-log-prefix", NOTES.name]
        notes_log = run_docker(*read_log, env=environment).stdout.splitlines()
        mounts = read_mounts(project)
    finally:
        down = ["docker", *compose, "down", "--volumes", "--remove-orphans"]
        subprocess.run(down, env=environment, capture_output=True, check=False)
    printed = json.loads(played.stdout.strip().splitlines()[-1])
    printed["probes"] |= in_services
    calls = [GatewayCall.model_validate_json(line) for line in call_log.read_text().splitlines()]
    return StackRun(
        calls=calls,
        tokens=tokens,
        containers=containers,
        agent_net=f"{project}_{AGENT_NETWORK}",
        agent_net_gateway=agent_net.gateway,
        service_secrets=service_secrets,
        notes_log=notes_log,
        mounts=mounts,
        **printed,
    )


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
def test_probe_prints_an_outcome_the_reach_table_allows(
    stack_run: StackRun,
    row: str,
    allowed: set[str],
) -> None:
    outcome = stack_run.read_outcome(row)

    assert outcome in allowed


def test_the_harness_probe_finds_the_harness_in_the_episode(stack_run: StackRun) -> None:
    found = json.loads(stack_run.episode_harness)

    assert (bool(found["modules"]), bool(found["paths"])) == (True, True)


def test_the_sandbox_runs_a_company_repos_tests_green(stack_run: StackRun) -> None:
    tested = stack_run.run_tests

    assert tested["returncode"] == 0


def test_the_sandbox_holds_only_its_own_token(stack_run: StackRun) -> None:
    held = stack_run.read_outcome(OWN_TOKEN_ROW)

    own_hash = hashlib.sha256(stack_run.tokens[AGENT].encode()).hexdigest()
    assert held == json.dumps([[TOKEN_FILE], own_hash])


def test_the_gateway_records_each_agents_sandbox_by_its_container_name(stack_run: StackRun) -> None:
    refused = [call for call in stack_run.calls if call.path == f"/{NOT_ALLOWED_PATH}"]

    callers = [(call.status, call.caller) for call in refused]

    assert callers == [
        (HTTPStatus.FORBIDDEN, f"{stack_run.containers[sandbox_service(agent)]}.{stack_run.agent_net}")
        for agent in (AGENT, OTHER_AGENT)
    ]


def test_a_reset_of_every_sandbox_leaves_only_the_checkout_it_keeps(stack_run: StackRun) -> None:
    checkouts = stack_run.checkouts

    listed = (checkouts["before_reset"], checkouts["after_reset"])

    assert listed == (checkouts["seeded"], checkouts["kept"])


def test_the_episode_runs_a_command_in_another_agents_sandbox_only_with_that_sandboxs_token(
    stack_run: StackRun,
) -> None:
    calls = stack_run.episode_calls

    assert calls == {"own_token": 0, "wrong_token": HTTPStatus.UNAUTHORIZED}


def test_a_reset_ends_a_command_still_running_in_the_sandbox(stack_run: StackRun) -> None:
    ended = stack_run.left_running_at_reset

    assert ended["returncode"] == -signal.SIGKILL


def test_nothing_listens_in_the_episode_container_beyond_its_loopback(stack_run: StackRun) -> None:
    listening = stack_run.episode_listening

    assert listening == []


def test_the_host_holds_no_address_on_agent_net(stack_run: StackRun) -> None:
    held = stack_run.agent_net_gateway

    assert held is None


def test_the_episode_container_holds_no_key(stack_run: StackRun) -> None:
    held = stack_run.episode_environment_key

    assert held == "absent"


def test_the_episode_container_holds_every_sandbox_token_and_no_other_secret(stack_run: StackRun) -> None:
    held = stack_run.episode_secrets

    assert held == [
        "sandbox_token_agent_main",
        "sandbox_token_controlplane_agent",
        "sandbox_token_datapipe_agent",
        "sandbox_token_distill_agent",
        "sandbox_token_eval_agent",
        "sandbox_token_jobsvc_agent",
        "sandbox_token_serving_agent",
    ]


def test_a_sandbox_holds_the_notes_credential_only_when_its_agent_is_granted_it(stack_run: StackRun) -> None:
    held = stack_run.list_secrets()

    credentials = {
        container: [name for name in names if name.startswith(CREDENTIAL_PREFIX)]
        for container, names in held.items()
    }
    assert credentials == {
        **{sandbox_service(agent.id): [NOTES_CREDENTIAL] for agent in CONFIG.agents},
        sandbox_service(AGENT_WITHOUT_CREDENTIAL): [],
        EPISODE_SERVICE: [],
        GATEWAY_SERVICE: [],
        NOTES.name: [NOTES_CREDENTIAL],
    }


def test_the_gateway_holds_the_key_and_nothing_else(stack_run: StackRun) -> None:
    held = stack_run.service_secrets[GATEWAY_SERVICE]

    assert held == [KEY_SECRET_NAME]


def test_the_notes_board_holds_exactly_its_credential(stack_run: StackRun) -> None:
    held = stack_run.service_secrets[NOTES.name]

    assert held == [NOTES_CREDENTIAL]


def test_the_notes_board_log_names_the_credential_each_request_carried(stack_run: StackRun) -> None:
    log = stack_run.notes_log

    listings = sorted(line for line in log if line.startswith("GET /notes "))
    assert listings == ["GET /notes 200 notes", "GET /notes 401 none"]


def test_no_container_of_the_project_mounts_the_docker_socket(stack_run: StackRun) -> None:
    mounts = stack_run.mounts

    socket_mounts = {
        service: [path for path in paths if DOCKER_SOCKET in path] for service, paths in mounts.items()
    }
    assert socket_mounts == {
        service: []
        for service in [
            GATEWAY_SERVICE,
            EPISODE_SERVICE,
            NOTES.name,
            *(sandbox_service(agent.id) for agent in CONFIG.agents),
        ]
    }
