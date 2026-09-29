"""A run config the harness cannot run fails at load, naming what is wrong, before any work starts."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import scenarios.loader
import yaml
from loc_arena import stack_episode
from loc_arena.config import ConfigError, load_run_config
from loc_arena.harness import load_protocol
from loc_arena.stack_episode import run_in_stack, run_runner_phase

from tests.unit._monitor_support import SHIPPED_MONITORS, write_monitors_file
from tests.unit._stack_services import serve_stack

RUN = "configs/aurora-efficiency.deterministic.yaml"
TEAM_SOURCE = "configs/aurora-efficiency.yaml"  # the file that declares the run's agents
MODELS = "models.cheap.yaml"
ROOT_AGENT = "agent-main"
SCENARIO = "aurora_efficiency"
EMPTY_CODEBASE = "nowhere"
MISSING_PROMPT = "missing-template.txt"
TYPO = "maxx"
ModelRoles = dict[str, dict[str, object]]
Aggregation = dict[str, object]
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


@pytest.mark.parametrize(
    ("override", "misspelled"),
    [
        ("episode: {orchestrator_turn_budjet: 5}", "orchestrator_turn_budjet"),
        ("side_task: {thresholds: {loop_len_flor: 3}}", "loop_len_flor"),
        ("side_task: {verifer: kill_chain_v1}", "verifer"),
        ("covert: {enabeld: true}", "enabeld"),
        ("logging: {agent_transcripts: false}", "agent_transcripts"),
        ("agent_loop: {tool_result_max_char: 10}", "tool_result_max_char"),
    ],
    ids=["episode", "side_task.thresholds", "side_task", "covert", "logging", "agent_loop"],
)
def test_a_misspelled_key_in_a_typed_block_fails_at_load(
    tmp_path: Path,
    override: str,
    misspelled: str,
) -> None:
    shutil.copytree("configs", tmp_path / "configs")
    run = tmp_path / "configs" / "run.yaml"
    run.write_text(f"extends: {Path(RUN).name}\n{override}\n")

    with pytest.raises(ConfigError, match=misspelled):
        load_run_config(run)


def test_a_misspelled_key_in_an_agent_fails_at_load(tmp_path: Path) -> None:
    run = _run_with_team(tmp_path, lambda team: team[1].update({"brief_rf": "prompts_inline.brief_serving"}))

    with pytest.raises(ConfigError, match="brief_rf"):
        load_run_config(run)


def test_a_misspelled_key_in_a_model_role_fails_at_load(tmp_path: Path) -> None:
    run = _configs_with_model_roles(tmp_path, lambda roles: roles["teacher"].update({"max_token": 64}))

    with pytest.raises(ConfigError, match="max_token"):
        load_run_config(run)


def _monitors_file_naming_a_missing_prompt(directory: Path) -> str:
    """A monitors file whose one live monitor names a prompt template that does not exist."""
    return str(write_monitors_file(directory, mode="live", prompt_ref=str(directory / MISSING_PROMPT)))


def test_a_runner_phase_whose_monitors_file_cannot_load_fails_before_the_episode_opens(
    tmp_path: Path,
) -> None:
    config = load_run_config(RUN)
    served = serve_stack(tmp_path, config)
    monitors = _monitors_file_naming_a_missing_prompt(tmp_path)

    with pytest.raises(ConfigError, match=MISSING_PROMPT):
        run_runner_phase(
            config,
            served.services,
            robust=True,
            output_directory=tmp_path / "runner",
            mirror_root=served.gateway.mirror_root,
            monitors_path=monitors,
        )
    assert not served.gateway.sealed_root.exists()


class StackBroughtUpError(RuntimeError):
    """The run brought its compose stack up."""


def _refuse_to_bring_a_stack_up(*arguments: object, **options: object) -> None:
    raise StackBroughtUpError


def test_a_stack_run_whose_monitors_file_cannot_load_fails_before_its_stack_comes_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stack_episode, "docker_available", lambda: True)
    monkeypatch.setattr(stack_episode, "up", _refuse_to_bring_a_stack_up)
    monkeypatch.setattr(
        stack_episode,
        "DEFAULT_MONITORS_PATH",
        _monitors_file_naming_a_missing_prompt(tmp_path),
    )

    with pytest.raises(ConfigError, match=MISSING_PROMPT):
        run_in_stack(RUN, "attack", out_root=tmp_path / "logs", dotenv_path=None)


def _monitors_file_with_aggregation(directory: Path, edit: Callable[[Aggregation], object]) -> str:
    """The shipped monitors file with its scoring.aggregation block as ``edit`` changed it."""
    document = yaml.safe_load(Path(SHIPPED_MONITORS).read_text())
    aggregation: Aggregation = document["scoring"]["aggregation"]
    edit(aggregation)
    path = directory / "monitors.yaml"
    path.write_text(yaml.safe_dump(document))
    return str(path)


@pytest.mark.parametrize("key", ["method", "per_monitor_reduce"])
def test_a_misspelled_aggregation_method_fails_at_load(tmp_path: Path, key: str) -> None:
    monitors = _monitors_file_with_aggregation(tmp_path, lambda aggregation: aggregation.update({key: TYPO}))

    with pytest.raises(ConfigError, match=f"scoring.aggregation.{key}"):
        load_protocol(load_run_config(RUN), monitors)


@pytest.mark.parametrize(
    ("field", "value"),
    [("mode", "lvie"), ("tap", ["actoin"])],
    ids=["a live monitor's mode", "a monitor's tap"],
)
def test_a_misspelled_monitor_mode_or_tap_fails_at_load(tmp_path: Path, field: str, value: object) -> None:
    monitors = write_monitors_file(tmp_path, **{field: value})

    with pytest.raises(ConfigError, match=field):
        load_protocol(load_run_config(RUN), str(monitors))


def test_a_misspelled_per_monitor_reduce_of_one_monitor_fails_at_load(tmp_path: Path) -> None:
    monitors = write_monitors_file(tmp_path, per_monitor_reduce=TYPO)

    with pytest.raises(ConfigError, match="per_monitor_reduce"):
        load_protocol(load_run_config(RUN), str(monitors))
