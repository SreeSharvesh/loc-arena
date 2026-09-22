"""The scorer/verifier registries dispatch BY NAME (config over code).

Loading ``aurora-efficiency.yaml`` runs the scenario's registrations; the harness then resolves
``config.main_task.scorer`` and ``config.side_task.verifier`` through the registries, and an unknown name is
a loud failure, not a silent no-op.
"""

from __future__ import annotations

import pytest
from loc_arena.config import ConfigError, load_run_config
from loc_arena.registry import (
    RegistryError,
    get_scorer,
    get_verifier,
    is_scorer,
    is_verifier,
)

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def test_task_names_resolve_through_the_registries() -> None:
    assert CFG.main_task["scorer"] == "aurora_pipeline_cost"
    assert CFG.side_task.verifier == "kill_chain_v1"
    # the names dispatch to callables, not to hardcoded imports
    assert callable(get_scorer(CFG.main_task["scorer"]))
    assert callable(get_verifier(CFG.side_task.verifier))
    assert is_scorer("aurora_pipeline_cost") and is_verifier("kill_chain_v1")


def test_unknown_name_raises_registry_error_with_known_names() -> None:
    with pytest.raises(RegistryError) as ei:
        get_scorer("no_such_scorer")
    assert "aurora_pipeline_cost" in str(ei.value)  # the error lists what IS registered
    with pytest.raises(RegistryError) as ev:
        get_verifier("no_such_verifier")
    assert "kill_chain_v1" in str(ev.value)


def test_config_fails_loud_on_an_unknown_scorer_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A real run config with only the scorer name swapped to an unregistered one loads through the same path
    # (extends env.default, resolves the scenario) and fails LOUD at the registry check, not silently.
    from pathlib import Path

    src = Path("configs/aurora-efficiency.yaml").read_text()
    bad = tmp_path / "bad.yaml"
    bad.write_text(src.replace("scorer: aurora_pipeline_cost", "scorer: not_registered"))
    with pytest.raises(ConfigError) as ei:
        load_run_config(str(bad), configs_dir="configs")  # extends/models resolve against real configs/
    assert "not_registered" in str(ei.value) and "scorer" in str(ei.value)
