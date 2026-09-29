"""Grants to chosen agents hold in a real stack: a volume only a group holds, and a network per sandbox."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from itertools import combinations
from pathlib import Path

import pytest
from loc_arena.compose_document import PROJECT_DIRECTORY
from loc_arena.compose_stack import EpisodeStack, docker_available, run_in_runner, teardown, up
from loc_arena.config import RunConfig, load_run_config
from loc_arena.stack.constants import (
    GATEWAY_EDGE_HOSTNAME,
    HEALTH_ROUTE,
    build_sandbox_service_name,
    build_service_url,
)

from tests.integration._docker_support import has_mount_at, share_a_network

pytestmark = pytest.mark.integration  # every test here brings the stack up

BASE_RUN_CONFIG = PROJECT_DIRECTORY / "configs" / "aurora-efficiency.deterministic.yaml"
POOL = ("serving-agent", "distill-agent")
OUTSIDER = "eval-agent"
BOARD = Path("/board")
EXPERIMENT = f"""\
extends: {BASE_RUN_CONFIG.name}
agent_groups: {{pool: [{", ".join(POOL)}]}}
networks: {{agent-net: {{per_agent: true}}}}
volumes: {{board: {{mount_path: {BOARD}, size_bytes: 1048576, read_write: [{{groups: [pool]}}]}}}}
"""
SANDBOXES = [build_sandbox_service_name(agent.id) for agent in load_run_config(BASE_RUN_CONFIG).agents]
# Python run in a container: prints {"<url>": <whether GET answered 200>} for each URL of its arguments.
HEALTH_PROBE = """\
import json, sys, urllib.request

def answers(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return response.status == 200
    except OSError:
        return False

print(json.dumps({url: answers(url) for url in sys.argv[1:]}))
"""
# Python run in a container: prints whether the host name of its argument resolves.
RESOLVE_PROBE = """\
import json, socket, sys

try:
    socket.getaddrinfo(sys.argv[1], None)
    resolves = True
except OSError:
    resolves = False
print(json.dumps({"resolves": resolves}))
"""


@pytest.fixture(scope="module")
def experiment(tmp_path_factory: pytest.TempPathFactory) -> RunConfig:
    run_file = tmp_path_factory.mktemp("experiment") / "grants.yaml"
    run_file.write_text(EXPERIMENT)
    return load_run_config(run_file, configs_dir=BASE_RUN_CONFIG.parent)


@pytest.fixture(scope="module")
def stack(experiment: RunConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[EpisodeStack]:
    if not docker_available():
        pytest.skip("docker daemon unavailable")
    episode_stack = up(
        experiment,
        project=f"locarena-grants-{secrets.token_hex(3)}",
        workdir=tmp_path_factory.mktemp("stack"),
    )
    try:
        yield episode_stack
    finally:
        teardown(episode_stack)


def _health_url(host: str, port: int) -> str:
    return build_service_url(host, port) + HEALTH_ROUTE


def _probe(stack: EpisodeStack, service: str, source: str, *arguments: str) -> dict[str, bool]:
    result = stack.exec(service, ["python", "-c", source, *arguments])
    return json.loads(result.stdout)


def test_a_sandbox_outside_the_group_has_no_board_mounted(stack: EpisodeStack) -> None:
    mounted = has_mount_at(stack, build_sandbox_service_name(OUTSIDER), BOARD.as_posix())

    assert mounted is False


def test_a_group_member_reads_what_another_member_wrote_on_the_board(stack: EpisodeStack) -> None:
    writer, reader = (build_sandbox_service_name(agent_id) for agent_id in POOL)
    note = BOARD / f"note-{secrets.token_hex(3)}"
    stack.exec(writer, ["python", "-c", f"open({note.as_posix()!r}, 'w').write('from the pool')"])

    read = stack.exec(reader, ["cat", note.as_posix()])

    assert read.stdout == "from the pool"


def test_with_per_agent_networks_no_two_sandboxes_share_a_network(stack: EpisodeStack) -> None:
    shared = {
        (first, second)
        for first, second in combinations(SANDBOXES, 2)
        if share_a_network(stack, first, second)
    }

    assert shared == set()


@pytest.mark.parametrize("other", [build_sandbox_service_name(agent_id) for agent_id in (POOL[1], OUTSIDER)])
def test_with_per_agent_networks_a_sandbox_cannot_resolve_another_agents_sandbox(
    stack: EpisodeStack,
    other: str,
) -> None:
    report = _probe(stack, build_sandbox_service_name(POOL[0]), RESOLVE_PROBE, other)

    assert report == {"resolves": False}


def test_with_per_agent_networks_a_sandbox_still_reaches_the_edge(
    stack: EpisodeStack,
    experiment: RunConfig,
) -> None:
    url = _health_url(GATEWAY_EDGE_HOSTNAME, experiment.settings.gateway.edge_port)

    report = _probe(stack, build_sandbox_service_name(OUTSIDER), HEALTH_PROBE, url)

    assert report == {url: True}


def test_with_per_agent_networks_the_runner_reaches_every_sandbox(
    stack: EpisodeStack,
    experiment: RunConfig,
) -> None:
    urls = [_health_url(sandbox, experiment.settings.gateway.execution_port) for sandbox in SANDBOXES]

    result = run_in_runner(stack, ["python", "-c", HEALTH_PROBE, *urls])

    assert json.loads(result.stdout.strip().splitlines()[-1]) == dict.fromkeys(urls, True)
