"""The root settings: every group frozen, strict and described; validated from a run config."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from loc_arena.config import ConfigError, load_run_config, load_settings
from loc_arena.settings import LocArenaSettings
from pydantic import BaseModel, JsonValue, ValidationError

CONFIGS = Path(__file__).parents[2] / "configs"
BASE_RUN_CONFIG = "aurora-efficiency.yaml"
OVERRIDDEN_QUOTA = 7


def _collect_groups(model: type[BaseModel]) -> list[type[BaseModel]]:
    annotations = [field.annotation for field in model.model_fields.values()]
    nested = [a for a in annotations if isinstance(a, type) and issubclass(a, BaseModel)]
    return [model, *(group for annotation in nested for group in _collect_groups(annotation))]


SETTINGS_GROUPS = _collect_groups(LocArenaSettings)


def _write_run_config(directory: Path, settings_block: str) -> Path:
    run_config = directory / "run.yaml"
    run_config.write_text(f"extends: {BASE_RUN_CONFIG}\n{settings_block}\n")
    return run_config


@pytest.mark.parametrize("group", SETTINGS_GROUPS, ids=lambda group: group.__name__)
def test_a_settings_group_is_frozen(group: type[BaseModel]) -> None:
    frozen = group.model_config.get("frozen")

    assert frozen is True


@pytest.mark.parametrize("group", SETTINGS_GROUPS, ids=lambda group: group.__name__)
def test_a_settings_group_forbids_unknown_keys(group: type[BaseModel]) -> None:
    extra = group.model_config.get("extra")

    assert extra == "forbid"


@pytest.mark.parametrize("group", SETTINGS_GROUPS, ids=lambda group: group.__name__)
def test_every_field_of_a_settings_group_is_described(group: type[BaseModel]) -> None:
    undescribed = [name for name, field in group.model_fields.items() if not field.description]

    assert undescribed == []


@pytest.mark.parametrize(
    "block",
    [
        {"inference": {"batch_generat": {}}},
        {"inference": {"batch_generate": {"teacher_token_quota": -1}}},
    ],
    ids=["unknown-key", "negative-quota"],
)
def test_an_invalid_settings_block_is_rejected(block: Mapping[str, Mapping[str, JsonValue]]) -> None:
    with pytest.raises(ValidationError):
        LocArenaSettings.model_validate(block)


def test_a_settings_value_in_a_run_config_overrides_the_default(tmp_path: Path) -> None:
    block = f"inference: {{batch_generate: {{teacher_token_quota: {OVERRIDDEN_QUOTA}}}}}"
    run_config = _write_run_config(tmp_path, block)

    settings = load_run_config(run_config, CONFIGS).settings

    assert settings.inference.batch_generate.teacher_token_quota == OVERRIDDEN_QUOTA


def test_the_settings_alone_load_with_a_run_configs_override(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, "gateway: {port: 9090}")

    settings = load_settings(run_config, CONFIGS)

    assert settings.gateway.port == 9090


def test_an_unknown_settings_key_in_a_run_config_is_a_config_error(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, "inference: {batch_generat: {}}")

    with pytest.raises(ConfigError, match="settings"):
        load_run_config(run_config, CONFIGS)
