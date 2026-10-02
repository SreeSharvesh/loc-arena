"""The provider key, the sealed logs and the grader's reference stay out of the runner's and agents' reach."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from loc_arena.compose_stack import (
    EpisodeStack,
    collect_run_output,
    docker_available,
    run_in_runner,
    teardown,
    up,
)
from loc_arena.config import load_run_config
from loc_arena.stack.constants import OPENROUTER_API_KEY_SECRET_NAME
from pydantic import SecretStr

from tests.integration._docker_support import IS_MOUNT_POINT_SOURCE, build_mount_point_probe

pytestmark = pytest.mark.integration  # every test here brings the stack up

CANARY = "sk-or-v1-CANARY-runner-isolation-5d1f"
CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
type ProbeResult = dict[str, bool | int | str | None]  # the JSON line a probe prints


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EpisodeStack]:
    if not docker_available():
        pytest.skip("docker daemon unavailable")
    episode_stack = up(
        CFG,
        project=f"locarena-runner-iso-{secrets.token_hex(3)}",
        workdir=tmp_path_factory.mktemp("stack"),
        secret_environment={OPENROUTER_API_KEY_SECRET_NAME.upper(): SecretStr(CANARY)},
    )
    try:
        yield episode_stack
    finally:
        teardown(episode_stack)


def _run_probe_in_runner(stack: EpisodeStack, code: str) -> ProbeResult:
    result = run_in_runner(stack, ["python", "-c", code])
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_key_is_a_secret_file_in_gateway_core(stack: EpisodeStack) -> None:
    secret = stack.exec("gateway_core", ["cat", "/run/secrets/openrouter_api_key"], check=False)

    assert secret.stdout.strip() == CANARY  # positive control: the key did load, where it should


def test_the_key_is_no_environment_variable_of_gateway_core(stack: EpisodeStack) -> None:
    environment = stack.exec("gateway_core", ["env"], check=False)

    assert CANARY not in environment.stdout  # a file, never an environment variable


def test_the_runner_cannot_see_the_key(stack: EpisodeStack) -> None:
    probe = (
        "import json, os, pathlib\n"
        f"canary = {CANARY!r}\n"
        "procs = [p for p in pathlib.Path('/proc').iterdir() if p.name.isdigit()]\n"
        "def environ(p):\n"
        "    try:\n"
        "        return (p / 'environ').read_bytes().decode(errors='replace')\n"
        "    except OSError:\n"
        "        return ''\n"
        "def holds(path):\n"
        "    try:\n"
        "        return canary in pathlib.Path(path).read_text()\n"
        "    except OSError:\n"
        "        return False\n"
        "secrets = pathlib.Path('/run/secrets')\n"
        "print(json.dumps({\n"
        "    'own_env': any(canary in v for v in os.environ.values()),\n"
        "    'any_proc_env': any(canary in environ(p) for p in procs),\n"
        "    'key_file': any(holds(p) for p in secrets.iterdir()) if secrets.exists() else False,\n"
        "    'key_var': 'OPENROUTER_API_KEY' in os.environ,\n"
        "}))\n"
    )

    found = _run_probe_in_runner(stack, probe)

    assert found == {"own_env": False, "any_proc_env": False, "key_file": False, "key_var": False}


def test_the_runner_has_no_internet_but_reaches_the_core(stack: EpisodeStack) -> None:
    probe = (
        "import json, socket, urllib.request\n"
        "def tcp(host, port):\n"
        "    s = socket.socket(); s.settimeout(4)\n"
        "    try:\n"
        "        s.connect((host, port)); return True\n"
        "    except OSError:\n"
        "        return False\n"
        "    finally:\n"
        "        s.close()\n"
        f"url = 'http://gateway-core:{CFG.settings.gateway.core_port}/health'\n"
        "health = json.load(urllib.request.urlopen(url, timeout=5))\n"
        "found = {'internet': tcp('1.1.1.1', 443), 'core': health['ok']}\n"
        "print(json.dumps(found | {'key': health['provider_configured']}))\n"
    )

    found = _run_probe_in_runner(stack, probe)

    # key: gateway_core loaded the canary from its secret file (pydantic-settings); the runner never sees it
    assert found == {"internet": False, "core": True, "key": True}


def test_the_runner_has_no_sealed_log(stack: EpisodeStack) -> None:
    found = _run_probe_in_runner(stack, build_mount_point_probe("/sealed"))

    assert found == {"mounted": False}


def test_the_edge_refuses_a_mirror_write_without_the_control_key(stack: EpisodeStack) -> None:
    probe = (
        "import json, urllib.request, urllib.error\n"
        "draft = {'ts': 1.0, 'actor_uid': 'agent-main', 'actor_role': 'untrusted', 'kind': 'message'}\n"
        "body = json.dumps({'episode_id': 'forged', 'draft': draft}).encode()\n"
        f"url = 'http://gateway-edge:{CFG.settings.gateway.edge_port}/mirror/0123456789abcdef/events'\n"
        "request = urllib.request.Request(url, body, {'Content-Type': 'application/json'})\n"
        "try:\n"
        "    status = urllib.request.urlopen(request, timeout=10).status\n"
        "except urllib.error.HTTPError as error:\n"
        "    status = error.code\n"
        "print(json.dumps({'status': status}))\n"
    )

    found = _run_probe_in_runner(stack, probe)

    assert found == {"status": 401}


def test_the_gateway_refuses_an_unknown_model_role_with_400(stack: EpisodeStack) -> None:
    probe = (
        "import json\n"
        "from contextlib import ExitStack\n"
        "import httpx\n"
        "from loc_arena.gateway.core_control_client import CoreControlClient\n"
        "from loc_arena.gateway.wiring import connect_stack_services\n"
        "from loc_arena.harness import load_config_by_name\n"
        "from loc_arena.stack.constants import GENERATE_ROUTE\n"
        "from loc_arena.stack.contracts import CovertInjection, EpisodeOpen, GenerateRequest, ModelRoute\n"
        "from loc_arena.stack.stack_secrets import load_container_secrets\n"
        "config = load_config_by_name('aurora-efficiency.deterministic')\n"
        "opening = EpisodeOpen(\n"
        "    run_config_name='probe', mode='honest', provider='deterministic',\n"
        "    models={'teacher': ModelRoute(model='stub', temperature=0.0, max_tokens=8)},\n"
        "    covert=CovertInjection(enabled=False, target_identity='none', objective_prompt=''),\n"
        ")\n"
        "request = GenerateRequest(prompt='hi', caller_identity='probe', role='no-such-role')\n"
        "with ExitStack() as resources:\n"
        "    services = connect_stack_services(config, load_container_secrets().control_key, resources)\n"
        "    control = CoreControlClient.open_episode(services.core, opening)\n"
        "    try:\n"
        "        status = services.edge.send('POST', GENERATE_ROUTE, request).status_code\n"
        "    except httpx.HTTPStatusError as error:\n"
        "        status = error.response.status_code\n"
        "    control.close()\n"
        "print(json.dumps({'status': status}))\n"
    )

    found = _run_probe_in_runner(stack, probe)

    assert found == {"status": 400}


# Run by the scripted serving-agent's bash in its own sandbox.
AGENT_PROBE = """\
import json, os, pathlib, socket

CANARY = {canary!r}


def connects(host, port):
    try:
        with socket.create_connection((host, port), timeout=4):
            return True
    except OSError:
        return False


def resolves(host):
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def writes(path):
    try:
        pathlib.Path(path).write_text("probe")
        return True
    except OSError:
        return False


def readable(path):
    try:
        pathlib.Path(path).read_bytes()
        return True
    except OSError:
        return False


{is_mount_point_source}

lines = open("/proc/self/status").read().splitlines()
status = dict(line.split(":\\t", 1) for line in lines if ":\\t" in line)
print(json.dumps({{
    "key_in_environment": any(CANARY in value for value in os.environ.values()),
    "key_variable_set": "OPENROUTER_API_KEY" in os.environ,
    "secrets_directory": pathlib.Path("/run/secrets").exists(),
    "dns": resolves("example.com"),
    "internet": connects("1.1.1.1", 443),
    "host_name_resolves": resolves("host.docker.internal"),
    "writes_app": writes("/app/probe.txt"),
    "runs_as_root": os.getuid() == 0,
    "effective_capabilities": status.get("CapEff", "").strip(),
    "no_new_privileges": status.get("NoNewPrivs", "").strip(),
    "docker_socket": pathlib.Path("/var/run/docker.sock").exists(),
    "grader_reference_readable": readable("/app/scenarios/aurora_efficiency/reference/reference.json"),
    "sealed_log_mounted": is_mount_point("/sealed"),
    "core_resolves": resolves("gateway-core"),
    "recorder_resolves": resolves("recorder"),
    "core_reachable": connects("gateway-core", {core_port}),
}}))
"""


def _scripted_agent_episode(command: str) -> str:
    return (
        "import dataclasses\n"
        "from contextlib import ExitStack\n"
        "from pathlib import Path\n"
        "from loc_arena.gateway.wiring import connect_stack_services\n"
        "from loc_arena.harness import apply_mode, load_config_by_name\n"
        "from loc_arena.stack_episode import run_runner_phase\n"
        "from loc_arena.scaffold.agent import ScriptedAgentPolicy\n"
        "from loc_arena.scaffold.tools import AgentAction\n"
        "import yaml\n"
        "from loc_arena.stack.stack_secrets import load_container_secrets\n"
        f"COMMAND = {command!r}\n"
        # heuristic monitors: the probe needs no model call, and none leaves the stack with the canary key
        "monitors = yaml.safe_load(Path('configs/monitors.yaml').read_text())\n"
        "for monitor in monitors['monitors']:\n"
        "    monitor['mode'] = 'heuristic'\n"
        "Path('/tmp/monitors.yaml').write_text(yaml.safe_dump(monitors))\n"
        "class ProbePolicy:\n"
        "    def __init__(self):\n"
        "        self.sent, self.output = False, ''\n"
        "    def next_actions(self, uid, turn, transcript):\n"
        "        if not self.sent:\n"
        "            self.sent = True\n"
        "            return [AgentAction('bash', {'command': COMMAND})]\n"
        "        self.output = transcript[-1]['result']['output']\n"
        "        return None\n"
        "probe = ProbePolicy()\n"
        "def policies(ctx, brief, tool_specs):\n"
        "    return probe if ctx.uid == 'serving-agent' else ScriptedAgentPolicy([])\n"
        "config = apply_mode(load_config_by_name('aurora-efficiency.deterministic'), 'honest')\n"
        "config = dataclasses.replace(config, policy='model', agent_transcript=False)\n"
        "with ExitStack() as resources:\n"
        "    services = connect_stack_services(config, load_container_secrets().control_key, resources)\n"
        "    run_runner_phase(config, services, robust=False, output_directory=Path('/tmp/probe'),\n"
        "                     monitors_path='/tmp/monitors.yaml', policy_factory=policies)\n"
        "print(probe.output.strip().splitlines()[-1])\n"
    )


@pytest.fixture(scope="module")
def agent_report(stack: EpisodeStack) -> ProbeResult:
    script = AGENT_PROBE.format(
        canary=CANARY,
        core_port=CFG.settings.gateway.core_port,
        is_mount_point_source=IS_MOUNT_POINT_SOURCE,
    )
    command = f"python - <<'PROBE'\n{script}PROBE\n"
    report = _run_probe_in_runner(stack, _scripted_agent_episode(command))
    assert report, "the agent's probe did not run"
    return report


def test_agent_code_cannot_read_the_key(agent_report: ProbeResult) -> None:
    found = {
        name: agent_report[name] for name in ("key_in_environment", "key_variable_set", "secrets_directory")
    }

    assert found == {"key_in_environment": False, "key_variable_set": False, "secrets_directory": False}


def test_agent_code_has_no_network_outside_the_stack(agent_report: ProbeResult) -> None:
    found = {name: agent_report[name] for name in ("dns", "internet", "host_name_resolves")}

    assert found == {"dns": False, "internet": False, "host_name_resolves": False}


def test_agent_code_cannot_resolve_the_core_or_the_recorder(agent_report: ProbeResult) -> None:
    found = {name: agent_report[name] for name in ("core_resolves", "recorder_resolves")}

    assert found == {"core_resolves": False, "recorder_resolves": False}


def test_agent_code_cannot_reach_the_core(agent_report: ProbeResult) -> None:
    reachable = agent_report["core_reachable"]

    assert reachable is False


def test_agent_code_cannot_write_to_the_image(agent_report: ProbeResult) -> None:
    writes = agent_report["writes_app"]

    assert writes is False


def test_agent_code_runs_unprivileged(agent_report: ProbeResult) -> None:
    found = {
        name: agent_report[name] for name in ("runs_as_root", "effective_capabilities", "no_new_privileges")
    }

    assert found == {
        "runs_as_root": False,
        "effective_capabilities": "0000000000000000",
        "no_new_privileges": "1",
    }


def test_agent_code_cannot_reach_the_docker_engine(agent_report: ProbeResult) -> None:
    socket = agent_report["docker_socket"]

    assert socket is False


def test_agent_code_cannot_read_the_grader_reference(agent_report: ProbeResult) -> None:
    readable = agent_report["grader_reference_readable"]

    assert readable is False


def test_agent_code_has_no_sealed_log(agent_report: ProbeResult) -> None:
    mounted = agent_report["sealed_log_mounted"]

    assert mounted is False


def test_a_run_sees_only_its_own_output_directory(stack: EpisodeStack, tmp_path: Path) -> None:
    (tmp_path / "this-run.txt").write_text("mine")
    probe = (
        "import json, pathlib\n"
        "logs = pathlib.Path('/app/logs')\n"
        "print(json.dumps({'entries': ','.join(sorted(p.name for p in logs.iterdir()))}))\n"
    )

    result = run_in_runner(stack, ["python", "-c", probe], output_directory=tmp_path)

    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"entries": "this-run.txt"}


def test_collecting_the_output_never_follows_links_the_runner_planted(
    stack: EpisodeStack,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"  # a host directory the runner cannot see
    outside.mkdir()
    (outside / "host-secret.txt").write_text(CANARY)
    staging, logs = tmp_path / "staging", tmp_path / "logs"
    staging.mkdir()
    probe = (
        "import json, os, pathlib\n"
        f"target = {str(outside)!r}\n"
        "out = pathlib.Path('/app/logs')\n"
        "(out / 'bundle').mkdir()\n"
        "(out / 'bundle' / 'scores.json').write_text('{}')\n"
        "os.symlink(target + '/host-secret.txt', out / 'bundle' / 'leak.txt')\n"
        "os.symlink(target, out / 'leak-dir')\n"
        "print(json.dumps({'planted': True}))\n"
    )
    planted = run_in_runner(stack, ["python", "-c", probe], output_directory=staging)
    assert planted.returncode == 0, planted.stderr[-2000:]

    dropped = collect_run_output(staging, logs)

    assert sorted(str(path) for path in dropped) == ["bundle/leak.txt", "leak-dir"]
    copied = sorted(str(path.relative_to(logs)) for path in logs.rglob("*"))
    assert copied == ["bundle", "bundle/scores.json"]
