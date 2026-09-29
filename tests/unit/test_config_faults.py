"""A run config the harness cannot run fails at load, naming what is wrong, before any work starts."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import scenarios.loader
import yaml
from loc_arena.config import ConfigError, load_run_config

RUN = "configs/aurora-efficiency.deterministic.yaml"
TEAM_SOURCE = "configs/aurora-efficiency.yaml"  # the file that declares the run's agents
MODELS = "models.cheap.yaml"
ROOT_AGENT = "agent-main"
SCENARIO = "aurora_efficiency"
EMPTY_CODEBASE = "nowhere"
ModelRoles = dict[str, dict[str, object]]
Team = list[dict[str, object]]


def _configs_with_model_roles(tmp_path: Path, edit: Callable[[ModelRoles], object]) -> Path:
    """The run file in a copy of configs/ whose models file's roles ``edit`` changed."""
    shutil.copytree("configs", tmp_path / "configs")
    models = tmp_path / "configs" / MODELS
    document = yaml.safe_load(models.read_text())
    roles: ModelRoles = document["roles"]
    edit(roles)
    models.write_text(yaml.safe_dump(document))
    return tmp_path / "configs" / Path(RUN).name


def _run_with_team(tmp_path: Path, edit: Callable[[Team], object]) -> Path:
    """A run file in a copy of configs/ that extends RUN with the shipped team as ``edit`` changed it."""
    shutil.copytree("configs", tmp_path / "configs")
    team: Team = yaml.safe_load(Path(TEAM_SOURCE).read_text())["agents"]
    edit(team)
    run = tmp_path / "configs" / "run.yaml"
    run.write_text(f"extends: {Path(RUN).name}\n" + yaml.safe_dump({"agents": team}))
    return run


@pytest.mark.parametrize("role", ["untrusted_agent", "teacher", "trusted_monitor"])
def test_a_models_file_without_a_role_the_run_calls_fails_at_load(tmp_path: Path, role: str) -> None:
    run = _configs_with_model_roles(tmp_path, lambda roles: roles.pop(role))

    with pytest.raises(ConfigError, match=role):
        load_run_config(run)


def test_a_team_without_its_root_agent_fails_at_load(tmp_path: Path) -> None:
    run = _run_with_team(tmp_path, lambda team: team.pop(0))

    with pytest.raises(ConfigError, match=ROOT_AGENT):
        load_run_config(run)


def test_a_team_that_repeats_an_agent_id_fails_at_load(tmp_path: Path) -> None:
    run = _run_with_team(tmp_path, lambda team: team.append(team[1]))

    with pytest.raises(ConfigError, match="serving-agent"):
        load_run_config(run)


def test_a_covert_target_outside_the_team_fails_at_load(tmp_path: Path) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    run = tmp_path / "configs" / "run.yaml"
    run.write_text(f"extends: {Path(RUN).name}\ncovert: {{target_identity: ghost-agent}}\n")

    with pytest.raises(ConfigError, match="ghost-agent"):
        load_run_config(run)


@pytest.mark.parametrize("codebase_exists", [False, True], ids=["missing", "holding no repository"])
def test_a_scenario_whose_codebase_holds_no_repository_fails_at_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codebase_exists: bool,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(Path("scenarios") / SCENARIO, project / "scenarios" / SCENARIO)
    pack = project / "scenarios" / SCENARIO / "scenario.yaml"
    pack.write_text(pack.read_text().replace("codebase: company", f"codebase: {EMPTY_CODEBASE}"))
    if codebase_exists:
        (project / EMPTY_CODEBASE).mkdir()
    monkeypatch.setattr(scenarios.loader, "SCENARIOS_ROOT", project / "scenarios")

    with pytest.raises(ConfigError, match=EMPTY_CODEBASE):
        load_run_config(RUN)


@pytest.mark.parametrize("rate", ["lots", "5", "-0.1"], ids=["a word", "above one", "below zero"])
def test_a_protocol_audit_budget_that_is_no_rate_fails_at_load(tmp_path: Path, rate: str) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    run = tmp_path / "configs" / "run.yaml"
    run.write_text(f"extends: {Path(RUN).name}\nprotocol: {{audit_budget_fpr: {rate}}}\n")

    with pytest.raises(ConfigError, match="audit_budget_fpr"):
        load_run_config(run)


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_tokens", 0), ("max_tokens", -1), ("model", ""), ("temperature", -0.5)],
    ids=["no output tokens", "negative output tokens", "an empty model id", "a negative temperature"],
)
def test_a_model_route_every_call_would_be_refused_on_fails_at_load(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    run = _configs_with_model_roles(tmp_path, lambda roles: roles["untrusted_agent"].update({field: value}))

    with pytest.raises(ConfigError, match=field):
        load_run_config(run)


@pytest.mark.parametrize("file_name", ["run.yaml", MODELS], ids=["the run file", "the models file"])
def test_a_config_file_that_is_no_yaml_fails_at_load_naming_it(tmp_path: Path, file_name: str) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    run = tmp_path / "configs" / "run.yaml"
    run.write_text(f"extends: {Path(RUN).name}\n")
    (tmp_path / "configs" / file_name).write_text("roles: [unclosed\n")

    with pytest.raises(ConfigError, match=file_name):
        load_run_config(run)
