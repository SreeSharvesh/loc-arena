"""The run reader: a pi agent that reads one finished run and reports, isolated like an agent's loop."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from loc_arena.compose_document import ComposeDocument
from loc_arena.config import load_run_config
from loc_arena.episode_stack import AGENT_NETWORK, GATEWAY_MODE_OPTION
from loc_arena.gateway.model_gateway import MODEL_GATEWAY_CONFIG_VARIABLE
from loc_arena.run_reader import (
    READER,
    READER_TOKEN_VARIABLE,
    issue_reader_secrets,
    read_report,
    read_run,
    render_reader_compose,
    summarize_run,
    write_reader_home,
)

from tests._stored_run import unpack_stored_run

CONFIG = load_run_config("configs/aurora-efficiency.fast.yaml")


def render(tmp_path: Path) -> ComposeDocument:
    return render_reader_compose(CONFIG, tmp_path / "run", tmp_path / "home", tmp_path / "work", ["pi"])


def test_the_reader_is_on_the_network_with_no_route_out_alone(tmp_path: Path) -> None:
    compose = render(tmp_path)

    networks = compose["services"][READER]["networks"]

    assert (networks, compose["networks"][AGENT_NETWORK]) == (
        [AGENT_NETWORK],
        {"internal": True, "driver_opts": {GATEWAY_MODE_OPTION: "isolated"}},  # no host address there
    )


def test_the_reader_has_a_sandboxes_limits(tmp_path: Path) -> None:
    reader, stack = render(tmp_path)["services"][READER], CONFIG.settings.stack

    limits = (reader["mem_limit"], reader["cpus"], reader["pids_limit"])

    assert limits == (stack.sandbox_memory_limit, stack.sandbox_cpus, stack.sandbox_pids_limit)


def test_the_reader_holds_its_own_token_and_no_provider_key(tmp_path: Path) -> None:
    reader = render(tmp_path)["services"][READER]

    held = (reader["secrets"], sorted(reader["environment"]), "env_file" in reader)

    assert held == (["reader_token"], ["HOME", "PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY"], False)


def test_the_reader_reads_the_run_and_writes_its_home_and_work_folders_alone(tmp_path: Path) -> None:
    reader = render(tmp_path)["services"][READER]

    assert reader["volumes"] == [
        f"{tmp_path / 'run'}:/data:ro",
        f"{tmp_path / 'home'}:/home/reader",
        f"{tmp_path / 'work'}:/work",
    ]


def reader_routes() -> tuple[dict[str, dict], str]:
    """The gateway's routes of a reading by name, and the hash of the reader's token."""
    secrets = issue_reader_secrets(CONFIG)
    config = yaml.safe_load(secrets[MODEL_GATEWAY_CONFIG_VARIABLE])
    token_hash = f"sha256:{hashlib.sha256(secrets[READER_TOKEN_VARIABLE].encode()).hexdigest()}"
    return {route["name"]: route for route in config["binds"][0]["listeners"][0]["routes"]}, token_hash


def test_the_readers_token_opens_its_own_route_alone() -> None:
    routes, token_hash = reader_routes()

    admitted = {
        name: [key["keyHash"] for key in route["policies"].get("apiKey", {}).get("keys", [])]
        for name, route in routes.items()
    }

    assert [name for name, hashes in admitted.items() if token_hash in hashes] == ["loop-reader"]
    assert admitted["loop-reader"] == [token_hash]


def test_the_readers_route_bounds_its_calls_and_sets_its_model() -> None:
    policies = reader_routes()[0]["loop-reader"]["policies"]

    bound = (
        [limit["maxTokens"] for limit in policies["localRateLimit"]],
        policies["ai"]["overrides"]["model"],
    )

    assert bound == ([CONFIG.settings.reader.model_calls], CONFIG.models[READER].model)


def test_the_readers_pi_calls_the_gateway_with_its_token_as_the_reader_roles_model(tmp_path: Path) -> None:
    command = write_reader_home(tmp_path, CONFIG, "agents")

    (provider,) = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())["providers"].values()
    assert (provider["baseUrl"], provider["apiKey"], [model["id"] for model in provider["models"]]) == (
        "http://gateway:8080/loop/reader/api/v1",
        "!cat /run/secrets/reader_token",
        [CONFIG.models[READER].model],
    )
    assert command[command.index("--tools") + 1] == "read,bash,write"
    assert "--no-mcp" in command


def test_a_runs_facts_count_what_its_records_hold(tmp_path: Path) -> None:
    run = unpack_stored_run(tmp_path)
    sealed = [json.loads(line) for line in (run / "episode" / "events.sealed.jsonl").read_text().splitlines()]
    jobs = sum(event["kind"] == "job" for event in sealed)
    verdicts = json.loads((run / "scores.json").read_text())["monitor_verdicts"]
    by_action_monitor = sum(verdict["monitor"] == "action_monitor" for verdict in verdicts)

    facts = summarize_run(run)

    assert f"| attack | {len(sealed)} | {jobs} |" in facts
    assert f"- Run verdicts: action_monitor: verdicts {by_action_monitor}," in facts


def test_a_folder_that_is_no_graded_run_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"scores\.json"):
        read_run(tmp_path, "agents", CONFIG)


def test_a_brief_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    run = unpack_stored_run(tmp_path)

    with pytest.raises(ValueError, match="no brief 'poems'"):
        read_run(run, "poems", CONFIG)


def test_a_run_config_with_no_reader_role_is_refused(tmp_path: Path) -> None:
    models = {role: spec for role, spec in CONFIG.models.items() if role != READER}

    with pytest.raises(ValueError, match="no 'reader' role"):
        write_reader_home(tmp_path, dataclasses.replace(CONFIG, models=models), "agents")


def test_a_report_that_is_a_link_is_refused(tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("the host's file")
    (tmp_path / "report.md").symlink_to(tmp_path / "secret")

    with pytest.raises(RuntimeError, match="no regular file"):
        read_report(tmp_path / "report.md", limit_bytes=1000)


def test_a_report_over_its_size_limit_is_refused(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text("x" * 1001)

    with pytest.raises(RuntimeError, match="1000 bytes"):
        read_report(tmp_path / "report.md", limit_bytes=1000)


def test_the_readers_route_refuses_another_model_and_a_list_of_fallback_models() -> None:
    (rule,) = reader_routes()[0]["loop-reader"]["policies"]["authorization"]["rules"]

    model = CONFIG.models[READER].model
    assert rule == {"allow": f'json(request.body).model == "{model}" && !has(json(request.body).models)'}


@pytest.mark.parametrize("make", [os.mkfifo, os.mkdir], ids=["a pipe", "a directory"])
def test_a_report_that_is_no_regular_file_is_refused_at_once(
    tmp_path: Path,
    make: Callable[[Path], None],
) -> None:
    make(tmp_path / "report.md")

    with pytest.raises(RuntimeError, match="no regular file"):
        read_report(tmp_path / "report.md", limit_bytes=1000)


def test_a_brief_named_by_a_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no brief"):
        write_reader_home(tmp_path, CONFIG, "../reader_briefs/agents")


def test_a_reading_under_a_run_with_its_own_agents_upstream_needs_the_first_key_alone(tmp_path: Path) -> None:
    config = load_run_config("configs/aurora-efficiency.explabs.yaml")

    compose = render_reader_compose(config, tmp_path / "run", tmp_path / "home", tmp_path / "work", ["pi"])

    assert "agents_upstream_key" not in compose["secrets"]


def test_a_run_directory_whose_readings_folder_is_a_link_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    run = unpack_stored_run(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (run / "readings").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(RuntimeError, match="holds a link"):
        read_run(run, "agents", CONFIG)
