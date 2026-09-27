"""The provider key reaches gateway_core only; the runner (agents and their code) cannot read it.

Brings the stack up with a canary key as the compose secret, proves the canary IS in gateway_core (positive
control), then probes the runner: its environment, every process's environment, the secrets path, and an
agent that writes a test to dump the key and runs it. Network facts are asserted from the rendered config
(membership) plus runtime reachability, per _docker_support's note on Docker Desktop.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import (
    EpisodeStack,
    collect_run_output,
    docker_available,
    render_compose,
    run_in_runner,
    teardown,
    up,
)
from pydantic import SecretStr

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")

CANARY = "sk-or-v1-CANARY-runner-isolation-5d1f"
CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
type ProbeResult = dict[str, bool | int | str | None]  # the JSON line a runner probe prints


@pytest.fixture(scope="module")
def stack() -> Iterator[EpisodeStack]:
    episode_stack = up(
        CFG,
        project="locarena-runner-iso",
        secret_environment={"OPENROUTER_API_KEY": SecretStr(CANARY)},
    )
    try:
        yield episode_stack
    finally:
        teardown(episode_stack)


def _run_probe_in_runner(stack: EpisodeStack, code: str) -> ProbeResult:
    result = run_in_runner(stack, ["python", "-c", code])
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_key_is_a_secret_file_in_gateway_core_only(stack: EpisodeStack) -> None:
    secret = stack.exec("gateway_core", ["cat", "/run/secrets/openrouter_api_key"], check=False)
    assert secret.stdout.strip() == CANARY  # positive control: the key did load, where it should
    env = stack.exec("gateway_core", ["env"], check=False)
    assert CANARY not in env.stdout  # a file, never an environment variable


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
        "print(json.dumps({\n"
        "    'own_env': any(canary in v for v in os.environ.values()),\n"
        "    'any_proc_env': any(canary in environ(p) for p in procs),\n"
        "    'secrets_dir': pathlib.Path('/run/secrets').exists(),\n"
        "    'key_var': 'OPENROUTER_API_KEY' in os.environ,\n"
        "}))\n"
    )
    found = _run_probe_in_runner(stack, probe)
    assert found == {"own_env": False, "any_proc_env": False, "secrets_dir": False, "key_var": False}


def test_the_runner_has_no_egress_but_reaches_the_endpoint(stack: EpisodeStack) -> None:
    assert render_compose(CFG)["services"]["runner"]["networks"] == [
        "control-net",
    ]  # membership: internal only
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
        "health = json.load(urllib.request.urlopen('http://gateway-core:4000/health', timeout=5))\n"
        "key_loaded = health['provider_configured']\n"
        "result = {'internet': tcp('1.1.1.1', 443), 'endpoint': health['ok'], 'key': key_loaded}\n"
        "print(json.dumps(result))\n"
    )
    # key: gateway_core loaded the canary from its secret file (pydantic-settings); the runner never sees it
    assert _run_probe_in_runner(stack, probe) == {"internet": False, "endpoint": True, "key": True}


def test_the_endpoint_refuses_models_outside_the_allowlist(stack: EpisodeStack) -> None:
    probe = (
        "import json, urllib.request, urllib.error\n"
        "def post(model):\n"
        "    req = {'model': model, 'prompt': 'hi', 'temperature': 0, 'max_tokens': 5}\n"
        "    body = json.dumps(req).encode()\n"
        "    req = urllib.request.Request('http://gateway-core:4000/generate', body, "
        "{'Content-Type': 'application/json'})\n"
        "    try:\n"
        "        return urllib.request.urlopen(req, timeout=60).status\n"
        "    except urllib.error.HTTPError as e:\n"
        "        return e.code\n"
        "codes = {'disallowed': post('openai/gpt-5'), 'allowed': post('meta-llama/llama-3.1-8b-instruct')}\n"
        "print(json.dumps(codes))\n"
    )
    codes = _run_probe_in_runner(stack, probe)
    assert codes["disallowed"] == 403
    # served with the loaded key: the canary is fake, so OpenRouter rejects it and the endpoint reports 502
    # (503 would mean no key was loaded)
    assert codes["allowed"] == 502


def test_agent_code_in_the_runner_cannot_read_the_key(stack: EpisodeStack) -> None:
    leak_test = (
        "import os, pathlib\n\n"
        "def test_probe():\n"
        "    key = os.environ.get('OPENROUTER_API_KEY', '<unset>')\n"
        "    seen = pathlib.Path('/run/secrets/openrouter_api_key').exists()\n"
        "    secret = 'secret-visible' if seen else 'no-secret'\n"
        "    pathlib.Path('probe_out.txt').write_text(key + '|' + secret)\n"
    )
    calls = [
        {
            "tool": "write_file",
            "args": {"path": "meridian-serving/tests/test_zz_probe.py", "content": leak_test},
        },
        {"tool": "run_tests", "args": {"repo": "meridian-serving"}},
        {"tool": "read_file", "args": {"path": "meridian-serving/probe_out.txt"}},
        {"tool": "finish"},
    ]
    probe = (
        "import dataclasses, json, re\n"
        "from pathlib import Path\n"
        "from loc_arena.config import load_run_config\n"
        "from loc_arena.gateway.core import DeterministicProvider, ProviderResult\n"
        "from loc_arena.harness import run_episode\n"
        f"replies = {[json.dumps(c) for c in calls]!r}\n"
        "class Scripted:\n"
        "    def generate(self, model, prompt, temperature, max_tokens):\n"
        "        m = re.search(r'You are agent ([^,]+),', prompt)\n"
        "        mine = m and m.group(1).rsplit('/', 1)[-1] == 'serving-agent'\n"
        '        text = replies.pop(0) if mine and replies else \'{"tool":"finish"}\'\n'
        "        return ProviderResult(text=text, prompt_tokens=1, completion_tokens=1)\n"
        "cfg = dataclasses.replace(load_run_config('configs/aurora-efficiency.deterministic.yaml'), "
        "policy='model', agent_transcript=False)\n"
        "bundle = run_episode(cfg, mode='honest', out_root=Path('/tmp/probe'), provider=Scripted(), "
        "calibration_provider=Scripted(), monitor_provider=DeterministicProvider(), write_report=False)\n"
        "dump = bundle / 'episode' / 'checkout' / 'meridian-serving' / 'probe_out.txt'\n"
        "print(json.dumps({'dump': dump.read_text() if dump.exists() else None}))\n"
    )
    assert _run_probe_in_runner(stack, probe) == {"dump": "<unset>|no-secret"}


def test_a_run_sees_only_its_own_output_directory(stack: EpisodeStack, tmp_path: Path) -> None:
    (tmp_path / "this-run.txt").write_text("mine")
    probe = (
        "import json, pathlib\n"
        "logs = pathlib.Path('/app/logs')\n"
        "print(json.dumps({'entries': ','.join(sorted(p.name for p in logs.iterdir()))}))\n"
    )
    result = run_in_runner(stack, ["python", "-c", probe], output_directory=tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
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
    result = run_in_runner(stack, ["python", "-c", probe], output_directory=staging)
    assert result.returncode == 0, result.stderr[-2000:]
    dropped = collect_run_output(staging, logs)
    assert sorted(str(path) for path in dropped) == ["bundle/leak.txt", "leak-dir"]
    copied = sorted(str(path.relative_to(logs)) for path in logs.rglob("*"))
    assert copied == ["bundle", "bundle/scores.json"]
    assert not any(CANARY in path.read_text() for path in logs.rglob("*") if path.is_file())
    assert not staging.exists()
