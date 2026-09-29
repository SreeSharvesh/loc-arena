"""A run config the harness cannot run fails at load, naming what is wrong, before any work starts."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from loc_arena.config import ConfigError, load_run_config

RUN = "configs/aurora-efficiency.deterministic.yaml"
MODELS = "models.cheap.yaml"
ModelRoles = dict[str, dict[str, object]]


def _configs_with_model_roles(tmp_path: Path, edit: Callable[[ModelRoles], object]) -> Path:
    """The run file in a copy of configs/ whose models file's roles ``edit`` changed."""
    shutil.copytree("configs", tmp_path / "configs")
    models = tmp_path / "configs" / MODELS
    document = yaml.safe_load(models.read_text())
    roles: ModelRoles = document["roles"]
    edit(roles)
    models.write_text(yaml.safe_dump(document))
    return tmp_path / "configs" / Path(RUN).name


@pytest.mark.parametrize("role", ["untrusted_agent", "teacher", "trusted_monitor"])
def test_a_models_file_without_a_role_the_run_calls_fails_at_load(tmp_path: Path, role: str) -> None:
    run = _configs_with_model_roles(tmp_path, lambda roles: roles.pop(role))

    with pytest.raises(ConfigError, match=role):
        load_run_config(run)
