"""Networks and volumes granted to chosen agents, to named groups of agents, or once per agent.

Each experiment is a run file extending the reference run config, as a researcher would write it, loaded by
the real config loader and rendered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.compose_document import REFERENCE_RUN_CONFIG, render_compose
from loc_arena.compose_schema import ComposeDocument, ComposeService
from loc_arena.config import load_run_config
from loc_arena.stack.constants import GATEWAY_EDGE_HOSTNAME, WORKSPACE_MOUNT_PATH, build_sandbox_service_name

AGENT_IDS = [agent.id for agent in load_run_config(REFERENCE_RUN_CONFIG).agents]
SANDBOXES = [build_sandbox_service_name(agent_id) for agent_id in AGENT_IDS]
POOL = ["serving-agent", "distill-agent"]
POOL_GROUP = f"agent_groups: {{pool: [{', '.join(POOL)}]}}\n"
BOARD_SIZE_BYTES = 1048576
PER_AGENT_NETWORKS = "networks: {agent-net: {per_agent: true}}\n"


def _render_experiment(tmp_path: Path, overlay: str) -> ComposeDocument:
    """Render a run file holding ``overlay`` that extends the reference run config."""
    run_file = tmp_path / "experiment.yaml"
    run_file.write_text(f"extends: {REFERENCE_RUN_CONFIG.name}\n{overlay}")
    return render_compose(load_run_config(run_file, configs_dir=REFERENCE_RUN_CONFIG.parent))


def _board(grants: str) -> str:
    return f"volumes: {{board: {{mount_path: /board, size_bytes: {BOARD_SIZE_BYTES}, {grants}}}}}\n"


def _mounts_of(document: ComposeDocument, source: str) -> dict[str, bool]:
    """Each service mounting volume ``source``, with whether it mounts it read-only."""
    return {
        name: mount["read_only"]
        for name, service in document["services"].items()
        for mount in service.get("volumes", [])
        if mount["source"] == source
    }


def _networks(service: ComposeService) -> set[str]:
    return set(service.get("networks", []))  # the names, whether rendered as a list or with aliases


def _members_of(document: ComposeDocument, network: str) -> set[str]:
    return {name for name, service in document["services"].items() if network in _networks(service)}


def test_a_volume_granted_to_one_agent_is_mounted_in_that_agents_sandbox_only(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, _board("read_write: [{agents: [eval-agent]}]"))

    mounts = _mounts_of(document, "board")

    assert mounts == {"sandbox-eval-agent": False}


def test_a_volume_granted_to_a_group_is_mounted_in_exactly_the_groups_sandboxes(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, POOL_GROUP + _board("read_write: [{groups: [pool]}]"))

    mounts = _mounts_of(document, "board")

    assert mounts == {build_sandbox_service_name(agent_id): False for agent_id in POOL}


def test_a_read_only_grant_to_an_agent_mounts_the_volume_read_only_in_its_sandbox(tmp_path: Path) -> None:
    grants = "read_write: [{groups: [pool]}], read_only: [{agents: [agent-main]}]"
    document = _render_experiment(tmp_path, POOL_GROUP + _board(grants))

    read_only = _mounts_of(document, "board")["sandbox-agent-main"]

    assert read_only is True


def test_an_extra_volume_is_a_tmpfs_of_its_size_owned_by_its_writers_user(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, _board("read_write: [{agents: [eval-agent]}]"))

    options = document["volumes"]["board"]["driver_opts"]

    uid, _, gid = document["services"]["sandbox-eval-agent"]["user"].partition(":")
    assert (options["type"], options["device"]) == ("tmpfs", "tmpfs")
    assert set(options["o"].split(",")) >= {f"uid={uid}", f"gid={gid}", f"size={BOARD_SIZE_BYTES}"}


def test_a_network_granted_to_a_group_is_joined_by_exactly_the_groups_sandboxes(tmp_path: Path) -> None:
    overlay = POOL_GROUP + "networks: {pool-net: {internal: true, sandboxes: {groups: [pool]}}}\n"
    document = _render_experiment(tmp_path, overlay)

    members = _members_of(document, "pool-net")

    assert members == {build_sandbox_service_name(agent_id) for agent_id in POOL}


def test_a_sandbox_granted_a_network_its_entry_lists_joins_it_once(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, "networks: {agent-net: {sandboxes: {agents: [eval-agent]}}}\n")

    networks = document["services"]["sandbox-eval-agent"]["networks"]

    assert networks == ["agent-net"]


def test_with_per_agent_networks_no_two_sandboxes_share_a_network(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, PER_AGENT_NETWORKS)

    services = document["services"]

    shared = {
        (first, second)
        for first in SANDBOXES
        for second in SANDBOXES
        if first < second and _networks(services[first]) & _networks(services[second])
    }
    assert shared == set()


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_with_per_agent_networks_a_sandbox_shares_a_network_with_the_edge(
    tmp_path: Path,
    sandbox: str,
) -> None:
    document = _render_experiment(tmp_path, PER_AGENT_NETWORKS)

    services = document["services"]

    assert _networks(services[sandbox]) & _networks(services["gateway_edge"])


def test_with_per_agent_networks_the_edge_answers_to_its_hostname_on_every_copy(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, PER_AGENT_NETWORKS)

    edge_networks = document["services"]["gateway_edge"]["networks"]

    assert isinstance(edge_networks, dict)
    agent_copies = {name: attachment for name, attachment in edge_networks.items() if name != "control-net"}
    assert agent_copies == {
        f"agent-net-{agent_id}": {"aliases": [GATEWAY_EDGE_HOSTNAME]} for agent_id in AGENT_IDS
    }


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_with_per_agent_networks_the_runner_shares_a_network_with_each_sandbox(
    tmp_path: Path,
    sandbox: str,
) -> None:
    document = _render_experiment(tmp_path, PER_AGENT_NETWORKS)

    services = document["services"]

    assert _networks(services[sandbox]) & _networks(services["runner"])


def test_a_per_agent_checkout_gives_each_sandbox_a_copy_of_its_own(tmp_path: Path) -> None:
    document = _render_experiment(tmp_path, "volumes: {checkout: {per_agent: true}}\n")

    sources = {
        name: mount["source"]
        for name in SANDBOXES
        for mount in document["services"][name]["volumes"]
        if mount["target"] == WORKSPACE_MOUNT_PATH.as_posix()
    }

    assert sources == {build_sandbox_service_name(agent_id): f"checkout-{agent_id}" for agent_id in AGENT_IDS}


def test_the_grader_reads_every_copy_of_a_per_agent_checkout_under_its_agents_directory(
    tmp_path: Path,
) -> None:
    document = _render_experiment(tmp_path, "volumes: {checkout: {per_agent: true}}\n")

    mounts = {
        mount["source"]: (mount["target"], mount["read_only"])
        for mount in document["services"]["grader"]["volumes"]
        if mount["type"] == "volume"
    }

    assert mounts == {
        f"checkout-{agent_id}": ((WORKSPACE_MOUNT_PATH / agent_id).as_posix(), True) for agent_id in AGENT_IDS
    }


@pytest.mark.parametrize(
    ("overlay", "path_and_error"),
    [
        (
            _board("read_write: [{agents: [eval-agnet]}]"),
            r"volumes\.board\.read_write\.0\.agents\.agents\.0\s+Value error, unknown agent 'eval-agnet'",
        ),
        (
            POOL_GROUP + _board("read_write: [{groups: [pol]}]"),
            r"volumes\.board\.read_write\.0\.agents\.groups\.0\s+Value error, unknown agent group 'pol'",
        ),
        (
            "agent_groups: {pool: [serving-agent, distil-agent]}\n",
            r"agent_groups\.pool\.1\s+Value error, unknown agent 'distil-agent'",
        ),
        (
            "networks: {pool-net: {sandboxes: {groups: [pool]}}}\n",
            r"networks\.pool-net\.sandboxes\.groups\.0\s+Value error, unknown agent group 'pool'",
        ),
    ],
)
def test_an_unknown_agent_or_group_fails_naming_the_path_of_the_bad_entry(
    tmp_path: Path,
    overlay: str,
    path_and_error: str,
) -> None:
    with pytest.raises(ValueError, match=path_and_error):
        _render_experiment(tmp_path, overlay)


def test_a_volume_granted_to_a_sandbox_both_to_write_and_to_read_is_refused(tmp_path: Path) -> None:
    overlay = "volumes: {checkout: {read_write: [sandbox], read_only: [grader, {agents: [eval-agent]}]}}\n"

    with pytest.raises(
        ValueError,
        match=r"volumes\.checkout grants sandbox-eval-agent both read-write and read-only",
    ):
        _render_experiment(tmp_path, overlay)


def test_recording_a_volume_in_the_mirror_is_refused_as_not_built(tmp_path: Path) -> None:
    overlay = _board("read_write: [sandbox], recorded_in_mirror: true")

    with pytest.raises(
        ValueError,
        match=r"volumes\.board\.recorded_in_mirror\s+Value error, recording a volume",
    ):
        _render_experiment(tmp_path, overlay)


def test_an_extra_volume_without_a_size_is_refused(tmp_path: Path) -> None:
    overlay = "volumes: {board: {mount_path: /board, read_write: [sandbox]}}\n"

    with pytest.raises(
        ValueError,
        match=r"volumes\.board is an extra volume .*needs a mount_path and a size_bytes",
    ):
        _render_experiment(tmp_path, overlay)


def test_an_extra_volume_with_no_writer_is_refused(tmp_path: Path) -> None:
    overlay = _board("read_only: [sandbox]")

    with pytest.raises(ValueError, match=r"volumes\.board is owned by its writers' user"):
        _render_experiment(tmp_path, overlay)


def test_a_per_agent_network_whose_copy_takes_a_declared_name_is_refused(tmp_path: Path) -> None:
    overlay = "networks: {agent-net: {per_agent: true}, agent-net-eval-agent: {internal: true}}\n"

    with pytest.raises(ValueError, match=r"networks\.agent-net is per agent, and its copies clash"):
        _render_experiment(tmp_path, overlay)
