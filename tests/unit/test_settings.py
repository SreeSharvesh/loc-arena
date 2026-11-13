"""The root settings: every group frozen, strict and described; validated from a run config."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from loc_arena.config import ConfigError, load_run_config, load_settings
from loc_arena.image_tag import derive_image_tag
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
        {"gateway": {"runner_models": ["", "deepseek/deepseek-v4.1-flash"]}},
        {"forge": {"ci_min_seconds": 5, "ci_max_seconds": 1}},
    ],
    ids=["unknown-key", "negative-quota", "empty-runner-model", "ci-wait-range-inverted"],
)
def test_an_invalid_settings_block_is_rejected(block: Mapping[str, Mapping[str, JsonValue]]) -> None:
    with pytest.raises(ValidationError):
        LocArenaSettings.model_validate(block)


def test_a_settings_value_in_a_run_config_overrides_the_default(tmp_path: Path) -> None:
    block = f"inference: {{batch_generate: {{teacher_token_quota: {OVERRIDDEN_QUOTA}}}}}"
    run_config = _write_run_config(tmp_path, block)

    settings = load_run_config(run_config, CONFIGS).settings

    assert settings.inference.batch_generate.teacher_token_quota == OVERRIDDEN_QUOTA


def _add_linked_worktree(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / ".git").write_text("gitdir: /a/clone/.git/worktrees/fix\n")  # what git writes in one
    return folder


def test_linked_worktrees_of_one_name_build_under_different_tags(tmp_path: Path) -> None:
    one, other = _add_linked_worktree(tmp_path / "a" / "fix"), _add_linked_worktree(tmp_path / "b" / "fix")

    tags = derive_image_tag(one), derive_image_tag(other)

    assert tags[0] != tags[1]


def test_a_linked_worktree_keeps_its_tag(tmp_path: Path) -> None:
    worktree = _add_linked_worktree(tmp_path / "fix")

    tags = derive_image_tag(worktree), derive_image_tag(worktree)

    assert tags[0] == tags[1]


@pytest.mark.parametrize(("folder", "named"), [("fix", "latest-fix-"), ("my fix", "latest-my-fix-")])
def test_a_linked_worktrees_tag_names_its_folder_in_characters_a_tag_may_hold(
    tmp_path: Path,
    folder: str,
    named: str,
) -> None:
    worktree = _add_linked_worktree(tmp_path / folder)

    tag = derive_image_tag(worktree)

    assert tag.startswith(named)


def test_a_clone_builds_under_latest(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    tag = derive_image_tag(tmp_path)

    assert tag == "latest"


def test_an_image_a_run_config_names_is_the_one_run(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, "stack: {image: registry.example/engine:1}")

    settings = load_run_config(run_config, CONFIGS).settings

    assert settings.stack.image == "registry.example/engine:1"


def test_the_settings_alone_load_with_a_run_configs_override(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, "gateway: {port: 9090}")

    settings = load_settings(run_config, CONFIGS)

    assert settings.gateway.port == 9090


@pytest.mark.parametrize(
    ("gateway", "named"),
    [
        ("{runner_model_aliases: {aurora-7b-v3: acme/other-model}}", "acme/other-model"),
        (
            "{runner_models: [deepseek/deepseek-v4.1-flash, acme/second-model], "
            "runner_model_aliases: {acme/second-model: deepseek/deepseek-v4.1-flash}}",
            "acme/second-model",
        ),
    ],
    ids=["maps-outside-runner-models", "renames-a-runner-model"],
)
def test_an_alias_that_is_not_a_new_name_for_a_runner_model_is_a_config_error(
    tmp_path: Path,
    gateway: str,
    named: str,
) -> None:
    run_config = _write_run_config(tmp_path, f"gateway: {gateway}")

    with pytest.raises(ConfigError, match=named):
        load_run_config(run_config, CONFIGS)


def test_an_unknown_settings_key_in_a_run_config_is_a_config_error(tmp_path: Path) -> None:
    run_config = _write_run_config(tmp_path, "inference: {batch_generat: {}}")

    with pytest.raises(ConfigError, match="settings"):
        load_run_config(run_config, CONFIGS)
