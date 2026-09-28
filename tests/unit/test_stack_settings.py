"""The root settings: every group frozen, strict and described; validated from a run config or compose."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from loc_arena.config import ConfigError, load_run_config
from loc_arena.stack.constants import SETTINGS_ENVIRONMENT_VARIABLE
from loc_arena.stack.settings import GatewaySettings, LocArenaSettings, load_settings_from_environment
from pydantic import BaseModel, JsonValue, ValidationError

CONFIGS = Path(__file__).parents[2] / "configs"
BASE_RUN_CONFIG = "aurora-efficiency.yaml"
NON_DEFAULT_PORT = 4100
OVERRIDDEN_MAX_MATCHES = 7
LONGER_THAN_THE_REPLY_TIMEOUT = 1_000.0  # seconds: above the default reply timeout
EQUAL_TIMEOUT = 10.0
EQUAL_TOOL_TIMEOUTS = {
    "run_tests_timeout_seconds": EQUAL_TIMEOUT,
    "run_benchmark_timeout_seconds": EQUAL_TIMEOUT,
    "bash_timeout_seconds": EQUAL_TIMEOUT,
}
PROVIDER_BUDGET_MILLISECONDS = 10_000
PROVIDER_BUDGET_SECONDS = 10.0  # the same budget, in the unit of the gateway's waits
LONGER_WAIT_SECONDS = 20.0
WAITS_ON_THE_PROVIDER = ("relay_timeout_seconds", "control_timeout_seconds")  # gateway fields


def _collect_groups(model: type[BaseModel]) -> list[type[BaseModel]]:
    """``model`` and every model nested in its fields at any depth: the root, its groups, their subgroups."""
    annotations = [field.annotation for field in model.model_fields.values()]
    nested = [a for a in annotations if isinstance(a, type) and issubclass(a, BaseModel)]
    return [model, *(group for annotation in nested for group in _collect_groups(annotation))]


SETTINGS_GROUPS = _collect_groups(LocArenaSettings)


def _group_name(group: type[BaseModel]) -> str:
    return group.__name__


def _block_with_a_provider_budget(budget_milliseconds: int, wait: str) -> dict[str, dict[str, JsonValue]]:
    """A provider budget of ``budget_milliseconds``; ``wait`` is PROVIDER_BUDGET_SECONDS, others longer."""
    gateway: dict[str, JsonValue] = dict.fromkeys(WAITS_ON_THE_PROVIDER, LONGER_WAIT_SECONDS)
    gateway[wait] = PROVIDER_BUDGET_SECONDS
    return {"gateway": gateway, "provider": {"backoff_max_elapsed_time_milliseconds": budget_milliseconds}}


def _write_run_config(directory: Path, settings_block: str) -> Path:
    """A run config extending the reference one with ``settings_block`` (YAML) appended."""
    run_config = directory / "run.yaml"
    run_config.write_text(f"extends: {BASE_RUN_CONFIG}\n{settings_block}\n")
    return run_config


@pytest.mark.parametrize("group", SETTINGS_GROUPS, ids=_group_name)
def test_a_settings_group_is_frozen(group: type[BaseModel]) -> None:
    frozen = group.model_config.get("frozen")

    assert frozen is True


@pytest.mark.parametrize("group", SETTINGS_GROUPS, ids=_group_name)
def test_a_settings_group_forbids_unknown_keys(group: type[BaseModel]) -> None:
    extra = group.model_config.get("extra")

    assert extra == "forbid"


@pytest.mark.parametrize("group", SETTINGS_GROUPS, ids=_group_name)
def test_every_field_of_a_settings_group_is_described(group: type[BaseModel]) -> None:
    undescribed = [name for name, field in group.model_fields.items() if not field.description]

    assert undescribed == []


@pytest.mark.parametrize(
    "block",
    [
        {"gateway": {"core_prot": NON_DEFAULT_PORT}},
        {"gateway": {"core_port": 0}},
        {"clock": {"start_ts": 5.0, "deadline_ts": 4.0}},
        {"clock": {"start_ts": 5.0, "deadline_ts": 5.0}},
        {"execution": {"run_tests_timeout_seconds": LONGER_THAN_THE_REPLY_TIMEOUT}},
        {"execution": {"run_benchmark_timeout_seconds": LONGER_THAN_THE_REPLY_TIMEOUT}},
        {"execution": {"bash_timeout_seconds": LONGER_THAN_THE_REPLY_TIMEOUT}},
        {"execution": {"reply_timeout_seconds": EQUAL_TIMEOUT} | EQUAL_TOOL_TIMEOUTS},
        {
            "grading": {
                "harness_file_names": ["../grade_bench.py"],
                "benchmark_file_name": "../grade_bench.py",
            },
        },
        {"grading": {"harness_file_names": [".."], "benchmark_file_name": ".."}},
        {"grading": {"benchmark_file_name": "reference.json"}},
    ],
    ids=[
        "unknown-key",
        "port-out-of-range",
        "deadline-before-start",
        "deadline-at-start",
        "suite-outlasts-the-reply-timeout",
        "benchmark-outlasts-the-reply-timeout",
        "bash-outlasts-the-reply-timeout",
        "reply-timeout-at-the-longest-tool-timeout",
        "harness-name-with-a-directory",
        "harness-name-that-is-a-parent",
        "benchmark-outside-the-harness-files",
    ],
)
def test_an_invalid_settings_block_is_rejected(block: Mapping[str, Mapping[str, JsonValue]]) -> None:
    with pytest.raises(ValidationError):
        LocArenaSettings.model_validate(block)


@pytest.mark.parametrize("wait", WAITS_ON_THE_PROVIDER)
def test_a_provider_budget_as_long_as_a_wait_on_the_provider_is_rejected(wait: str) -> None:
    block = _block_with_a_provider_budget(PROVIDER_BUDGET_MILLISECONDS, wait)

    with pytest.raises(ValidationError, match=wait):
        LocArenaSettings.model_validate(block)


@pytest.mark.parametrize("wait", WAITS_ON_THE_PROVIDER)
def test_a_provider_budget_just_below_a_wait_on_the_provider_is_accepted(wait: str) -> None:
    block = _block_with_a_provider_budget(PROVIDER_BUDGET_MILLISECONDS - 1, wait)

    LocArenaSettings.model_validate(block)  # raises ValidationError if refused


def test_settings_compose_renders_into_the_environment_load_back_unchanged() -> None:
    rendered = LocArenaSettings(gateway=GatewaySettings(core_port=NON_DEFAULT_PORT))
    environment = {SETTINGS_ENVIRONMENT_VARIABLE: rendered.model_dump_json()}

    loaded = load_settings_from_environment(environment)

    assert loaded == rendered


def test_a_settings_value_in_a_run_config_overrides_the_default(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, f"execution: {{max_matches: {OVERRIDDEN_MAX_MATCHES}}}")

    settings = load_run_config(run_config, CONFIGS).settings

    assert settings.execution.max_matches == OVERRIDDEN_MAX_MATCHES


def test_an_unknown_settings_key_in_a_run_config_is_a_config_error(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, f"execution: {{max_match: {OVERRIDDEN_MAX_MATCHES}}}")

    with pytest.raises(ConfigError, match="settings"):
        load_run_config(run_config, CONFIGS)
