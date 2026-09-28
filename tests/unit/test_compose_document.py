from __future__ import annotations

import copy
import dataclasses
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import scenarios.loader
from loc_arena.compose_document import (
    LISTEN_HOST,
    PROJECT_DIRECTORY,
    REFERENCE_COMPOSE_FILE,
    REFERENCE_RUN_CONFIG,
    SECRET_FILE_MODE,
    RunTopology,
    ServiceSpec,
    read_codebase,
    render_compose,
    render_reference_compose_file,
    render_service,
)
from loc_arena.compose_schema import ComposeService
from loc_arena.config import load_run_config
from loc_arena.stack.constants import (
    CODEBASE_BUILD_ARGUMENT,
    CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE,
    CONTROL_KEY_SECRET_NAME,
    GATEWAY_CORE_HOSTNAME,
    GATEWAY_EDGE_HOSTNAME,
    GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE,
    GRADER_HARNESS_MOUNT_PATH,
    GRADER_MEASURE_MODULE_MOUNT_PATH,
    HEALTH_ROUTE,
    IMAGE_TAG_ENVIRONMENT_VARIABLE,
    MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE,
    MIRROR_MOUNT_PATH,
    OPENROUTER_API_KEY_SECRET_NAME,
    RECORDER_HOSTNAME,
    SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE,
    SANDBOX_SERVICE_PREFIX,
    SEALED_MOUNT_PATH,
    SETTINGS_ENVIRONMENT_VARIABLE,
    WORKSPACE_MOUNT_PATH,
    build_sandbox_service_name,
)
from loc_arena.stack.settings import LocArenaSettings
from loc_arena.topology import load_stack_topology

CONFIG = load_run_config(REFERENCE_RUN_CONFIG)
SERVICES = render_compose(CONFIG)["services"]
SANDBOXES = sorted(build_sandbox_service_name(agent.id) for agent in CONFIG.agents)
AGENT_REACHABLE = [*SANDBOXES, "gateway_edge", "grader"]  # every container agent code can reach or run in
RFC_1123_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
type RawConfigEdit = Callable[[dict[str, Any]], object]  # changes a copy of the run config's raw mapping


def _networks(service: ComposeService) -> set[str]:
    return set(service.get("networks", []))  # the names, whether rendered as a list or with aliases


def _render_grader_built_from(codebase: str) -> ComposeService:
    topology = load_stack_topology(CONFIG.raw)
    spec = ServiceSpec(name="grader", config_name="grader", declaration=topology.services["grader"])
    run = RunTopology(
        settings=CONFIG.settings,
        topology=topology,
        service_names={},
        grading=None,
        codebase=codebase,
    )
    return render_service(spec, run)


def _render_edited(edit: RawConfigEdit) -> None:
    raw = copy.deepcopy(CONFIG.raw)
    edit(raw)
    render_compose(dataclasses.replace(CONFIG, raw=raw))


def test_one_sandbox_per_agent_named_as_an_rfc_1123_label() -> None:
    rendered = sorted(name for name in SERVICES if name.startswith(SANDBOX_SERVICE_PREFIX))
    assert rendered == SANDBOXES
    assert len(rendered) == len(CONFIG.agents) == 7
    assert all(RFC_1123_LABEL.fullmatch(name) for name in rendered)
    for agent in CONFIG.agents:
        environment = SERVICES[build_sandbox_service_name(agent.id)]["environment"]
        assert environment[SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE] == agent.id


@pytest.mark.parametrize("name", SANDBOXES)
def test_a_sandbox_is_on_agent_net_only_holds_no_secret_and_reaches_the_model_through_the_edge(
    name: str,
) -> None:
    sandbox = SERVICES[name]
    assert sandbox["networks"] == ["agent-net"]
    assert "secrets" not in sandbox
    edge_url = f"http://{GATEWAY_EDGE_HOSTNAME}:{CONFIG.settings.gateway.edge_port}"
    assert sandbox["environment"][GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE] == edge_url
    mounts = {mount["target"]: mount["read_only"] for mount in sandbox["volumes"]}
    assert mounts == {WORKSPACE_MOUNT_PATH.as_posix(): False, MIRROR_MOUNT_PATH.as_posix(): True}


def test_only_the_core_and_the_recorder_are_on_sealed_net_and_only_the_core_has_egress() -> None:
    assert {name for name, service in SERVICES.items() if "sealed-net" in _networks(service)} == {
        "gateway_core",
        "recorder",
    }
    assert {name for name, service in SERVICES.items() if "egress-net" in _networks(service)} == {
        "gateway_core",
    }


@pytest.mark.parametrize("name", ["grader", "evidence_reader"])
def test_the_grader_and_the_evidence_reader_have_no_network(name: str) -> None:
    assert SERVICES[name]["network_mode"] == "none"
    assert "networks" not in SERVICES[name]


def test_the_sealed_log_is_written_by_the_recorder_and_read_by_the_evidence_reader_only() -> None:
    access = {
        name: mount["read_only"]
        for name, service in SERVICES.items()
        for mount in service.get("volumes", [])
        if mount["source"] == "sealed_log" or mount["target"] == SEALED_MOUNT_PATH.as_posix()
    }
    assert access == {"recorder": False, "evidence_reader": True}
    assert _networks(SERVICES["runner"]) == {"agent-net", "control-net"}


@pytest.mark.parametrize("name", AGENT_REACHABLE)
def test_an_agent_reachable_container_is_hardened(name: str) -> None:
    service = SERVICES[name]
    uid, _, gid = service["user"].partition(":")
    assert uid.isdigit() and gid.isdigit() and int(uid) != 0  # non-root, whatever the image says
    assert service["read_only"] is True
    assert service["tmpfs"]
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["pids_limit"] > 0


@pytest.mark.parametrize("name", [*SANDBOXES, "grader"])
def test_where_agent_code_runs_the_tmp_tmpfs_is_capped_and_an_init_reaps_detached_processes(
    name: str,
) -> None:
    service = SERVICES[name]
    size = f"size={CONFIG.settings.docker.agent_tmpfs_size_bytes}"
    assert [entry for entry in service["tmpfs"] if entry.startswith("/tmp:")] == [f"/tmp:mode=1777,{size}"]
    assert service["init"] is True


def test_each_service_runs_the_image_target_the_design_gives_it() -> None:
    targets = {name: service["build"].get("target") for name, service in SERVICES.items()}
    sandbox_target = {name for name, target in targets.items() if target == "sandbox"}
    assert sandbox_target == {*SANDBOXES, "gateway_edge", "grader"}
    assert {name for name, target in targets.items() if target == "app"} == {
        "gateway_core",
        "recorder",
        "runner",
        "evidence_reader",
    }
    assert all(service["pull_policy"] == "never" for service in SERVICES.values())
    assert SERVICES["grader"]["command"] == ["python", "-m", "loc_arena.grader"]


def test_secrets_are_granted_only_to_the_core_the_edge_and_the_runner() -> None:
    granted = {
        name: {grant["source"] for grant in service["secrets"]}
        for name, service in SERVICES.items()
        if "secrets" in service
    }
    assert granted == {
        "gateway_core": {OPENROUTER_API_KEY_SECRET_NAME, CONTROL_KEY_SECRET_NAME},
        "gateway_edge": {CONTROL_KEY_SECRET_NAME},
        "runner": {CONTROL_KEY_SECRET_NAME},
    }


def test_each_grant_carries_only_the_fields_compose_applies_to_its_source() -> None:
    grants = {grant["source"]: grant for grant in SERVICES["gateway_core"]["secrets"]}
    provider_key = grants[OPENROUTER_API_KEY_SECRET_NAME]
    assert (provider_key["target"], provider_key["uid"], provider_key["gid"]) == (
        OPENROUTER_API_KEY_SECRET_NAME,
        "999",
        "999",
    )
    assert SERVICES["gateway_core"]["user"] == f"{provider_key['uid']}:{provider_key['gid']}"
    assert int(provider_key["mode"], 8) == SECRET_FILE_MODE == 0o400  # compose-go reads a string in base 8
    for service in ("gateway_core", "gateway_edge", "runner"):
        control_grants = [g for g in SERVICES[service]["secrets"] if g["source"] == CONTROL_KEY_SECRET_NAME]
        assert control_grants == [{"source": CONTROL_KEY_SECRET_NAME, "target": CONTROL_KEY_SECRET_NAME}]
    control_key_source = render_compose(CONFIG)["secrets"][CONTROL_KEY_SECRET_NAME]
    assert control_key_source["file"].startswith(f"${{{CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE}:?")


def test_every_service_runs_an_image_under_the_tag_its_episode_is_given() -> None:
    tags = {name: service["image"].partition(":")[2] for name, service in SERVICES.items()}

    assert all(tag.startswith(f"${{{IMAGE_TAG_ENVIRONMENT_VARIABLE}:?") for tag in tags.values()), tags


def test_an_environment_sourced_secret_in_a_read_only_container_is_refused() -> None:
    def grant_the_provider_key_to_the_edge(raw: dict[str, Any]) -> None:
        raw["services"]["gateway_edge"]["secrets"].append(OPENROUTER_API_KEY_SECRET_NAME)

    with pytest.raises(ValueError, match="read-only root"):
        _render_edited(grant_the_provider_key_to_the_edge)


@pytest.mark.parametrize(
    ("name", "factory", "port_setting", "hostname"),
    [
        ("gateway_core", "loc_arena.gateway.core_service:build_core_app", "core_port", GATEWAY_CORE_HOSTNAME),
        ("gateway_edge", "loc_arena.gateway.edge:build_edge_app", "edge_port", GATEWAY_EDGE_HOSTNAME),
        (
            "recorder",
            "loc_arena.services.recorder.app:build_recorder_app",
            "recorder_port",
            RECORDER_HOSTNAME,
        ),
        (SANDBOXES[0], "loc_arena.execution.app:build_execution_app", "execution_port", None),
    ],
)
def test_an_http_service_runs_its_factory_on_its_settings_port_and_answers_health_probes(
    name: str,
    factory: str,
    port_setting: str,
    hostname: str | None,
) -> None:
    service, docker = SERVICES[name], CONFIG.settings.docker
    port = getattr(CONFIG.settings.gateway, port_setting)
    assert service["command"] == ["uvicorn", "--factory", factory, "--host", LISTEN_HOST, "--port", str(port)]
    healthcheck = service["healthcheck"]
    assert f"http://localhost:{port}{HEALTH_ROUTE}" in healthcheck["test"][-1]
    assert (healthcheck["interval"], healthcheck["retries"]) == (
        f"{docker.healthcheck_interval_seconds}s",
        docker.healthcheck_retries,
    )
    networks = service["networks"]
    if hostname is not None:
        assert isinstance(networks, dict)
        assert all(attachment["aliases"] == [hostname] for attachment in networks.values())


def test_every_container_receives_the_run_settings_verbatim() -> None:
    header = CONFIG.settings.gateway.model_copy(update={"control_key_header": "X-$HOME-${KEY}"})
    config = dataclasses.replace(CONFIG, settings=CONFIG.settings.model_copy(update={"gateway": header}))
    for service in render_compose(config)["services"].values():
        rendered = service["environment"][SETTINGS_ENVIRONMENT_VARIABLE]
        assert "$" not in rendered.replace("$$", "")  # compose would interpolate a single $
        interpolated = rendered.replace("$$", "$")
        assert LocArenaSettings.model_validate_json(interpolated) == config.settings


def test_the_grader_reads_the_checkout_the_harness_files_and_the_measure_module_only() -> None:
    mounts = SERVICES["grader"]["volumes"]
    assert all(mount["read_only"] for mount in mounts)
    volumes = [mount for mount in mounts if mount["type"] == "volume"]
    assert [(mount["source"], mount["target"]) for mount in volumes] == [
        ("checkout", WORKSPACE_MOUNT_PATH.as_posix()),
    ]
    binds = [mount for mount in mounts if mount["type"] == "bind"]
    file_names = CONFIG.settings.grading.harness_file_names
    assert [mount["target"] for mount in binds] == [
        *((GRADER_HARNESS_MOUNT_PATH / n).as_posix() for n in file_names),
        GRADER_MEASURE_MODULE_MOUNT_PATH.as_posix(),
    ]
    for mount in binds:  # one file each (never reference/, which holds the sealed expected outputs)
        assert mount["bind"] == {"create_host_path": False}
        assert (PROJECT_DIRECTORY / mount["source"]).is_file()
        assert mount["source"].startswith("./")  # relative: the same render on every machine


def test_the_grader_is_given_the_measure_module_of_the_run_scenario() -> None:
    binds = [mount for mount in SERVICES["grader"]["volumes"] if mount["type"] == "bind"]

    sources = {
        mount["source"] for mount in binds if mount["target"] == GRADER_MEASURE_MODULE_MOUNT_PATH.as_posix()
    }

    assert sources == {f"./scenarios/{CONFIG.scenario}/measure.py"}


def test_the_grader_is_told_the_run_scorer() -> None:
    environment = SERVICES["grader"]["environment"]

    assert environment[MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE] == CONFIG.main_task["scorer"]


def test_only_the_grader_is_told_the_run_scorer() -> None:
    told = {
        name
        for name, service in SERVICES.items()
        if MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE in service["environment"]
    }

    assert told == {"grader"}


def test_an_image_built_from_another_codebase_is_given_it_as_its_build_argument() -> None:
    grader = _render_grader_built_from("scenarios/toy/codebase")

    assert grader["build"].get("args") == {CODEBASE_BUILD_ARGUMENT: "scenarios/toy/codebase"}


def test_a_run_whose_scenario_codebase_is_no_directory_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "scenarios" / "codeless").mkdir(parents=True)
    (tmp_path / "scenarios" / "codeless" / "scenario.yaml").write_text(
        "name: codeless\nscorer: s\nverifier: v\ncodebase: missing\n",
    )
    monkeypatch.setattr(scenarios.loader, "SCENARIOS_ROOT", tmp_path / "scenarios")

    with pytest.raises(ValueError, match="is not a directory"):
        read_codebase(dataclasses.replace(CONFIG, scenario="codeless"))


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (
            lambda raw: raw["services"]["gateway_edge"].update(read_only_root_filesytem=True),
            r"services\.gateway_edge\.read_only_root_filesytem\s+Extra inputs",
        ),
        (
            lambda raw: raw["services"]["gateway_edge"].update(port_setting="max_request_bytes"),
            r"services\.gateway_edge\s+Value error, port_setting must name a port",
        ),
        (
            lambda raw: raw["services"]["gateway_edge"].update(image="edge"),
            r"services\.gateway_edge\.image\s+Input should be 'app' or 'sandbox'",
        ),
        (
            lambda raw: raw["services"]["runner"]["depends_on_healthy"].append("gateway-core"),
            r"services\.runner\.depends_on_healthy\.3\s+Value error, unknown service 'gateway-core'",
        ),
        (
            lambda raw: raw["services"]["runner"]["secrets"].append("provider_key"),
            r"services\.runner\.secrets\.1\s+Value error, unknown secret 'provider_key'",
        ),
        (
            lambda raw: raw["services"]["sandbox"]["networks"].append("agent_net"),
            r"services\.sandbox\.networks\.1\s+Value error, unknown network 'agent_net'",
        ),
        (
            lambda raw: raw["volumes"]["sealed_log"]["read_only"].append("evidence-reader"),
            r"volumes\.sealed_log\.read_only\.1\s+Value error, unknown service 'evidence-reader'",
        ),
        (
            lambda raw: raw["volumes"].update(repos={"read_write": ["sandbox"]}),
            r"volumes\.repos\.\[key\]\s+Value error, no mount path",
        ),
        (
            lambda raw: raw["services"]["sandbox"].update(command=["bash"]),
            r"services\.sandbox\s+Value error, an app is served by uvicorn, so it takes no command",
        ),
    ],
)
def test_a_typo_in_the_topology_is_an_error_naming_its_path(edit: RawConfigEdit, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _render_edited(edit)


def test_the_committed_compose_file_is_a_fresh_render() -> None:
    rendered = render_reference_compose_file()
    assert REFERENCE_COMPOSE_FILE.read_text() == rendered, "run: uv run python -m loc_arena.compose_document"
    assert "&id" not in rendered  # no YAML anchor: every rendered value is its own object
