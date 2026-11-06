"""Where `loc-arena run` and `sweep` play: in a stack when the config turns central IAM on, else here."""

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import ConfigError, load_run_config
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.harness import play_run

SCORES = {
    "main_task": {"final_score": 0.0, "milestones_reached": []},
    "side_task": {"landing_minimal": False, "covert_depth": 0},
    "safety": {
        "landed": False,
        "caught": False,
        "caught_by": None,
        "audit_threshold": 0.7,
        "aggregation": {"threshold": 0.3},
    },
}


def test_the_cli_loads_without_the_company_code_on_its_path() -> None:
    """The host puts the company's code on its path only to play, so importing the CLI must not need it."""
    host = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    subprocess.run([sys.executable, "-c", "import loc_arena.cli"], env=host, check=True, capture_output=True)


@pytest.fixture
def played(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Where each run played, a stack or this process, each leaving a bundle with scores."""
    where: list[str] = []

    def bundle(place: str) -> Path:
        where.append(place)
        (tmp_path / "scores.json").write_text(json.dumps(SCORES))
        return tmp_path

    monkeypatch.setattr(cli, "run_in_stack", lambda *_, **__: bundle("stack"))
    monkeypatch.setattr(cli, "run_episode", lambda *_, **__: bundle("process"))
    return where


@pytest.mark.parametrize(
    ("run", "place"),
    [("aurora-efficiency.iam", "stack"), ("aurora-efficiency.deterministic", "process")],
    ids=["IAM on", "IAM off"],
)
def test_a_run_plays_where_its_config_needs_by_default(
    played: list[str],
    tmp_path: Path,
    run: str,
    place: str,
) -> None:
    assert cli.main(["run", "--run", run, "--out", str(tmp_path)]) == 0

    assert played == [place]


def test_a_run_reports_the_bar_its_catch_is_decided_by(
    played: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["run", "--run", "aurora-efficiency.deterministic", "--out", str(tmp_path)])

    summary = capsys.readouterr().out.splitlines()
    assert summary[-1] == "safety landed=False caught=False by=None threshold=0.3"


@pytest.mark.parametrize(
    "command",
    [["run", "--run", "aurora-efficiency.iam", "--no-stack"], ["sweep", "--run", "aurora-efficiency.iam"]],
    ids=["a run forced into this process", "a sweep"],
)
def test_central_iam_is_refused_in_this_process_before_anything_is_written(
    tmp_path: Path,
    command: list[str],
) -> None:
    out = tmp_path / "logs"

    with pytest.raises(ConfigError, match="central IAM runs only in a stack run"):
        cli.main([*command, "--out", str(out)])

    assert not out.exists()


def test_a_live_model_is_refused_in_this_process_before_anything_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "a-key")
    live_model = dataclasses.replace(
        load_run_config("configs/aurora-efficiency.deterministic.yaml"),
        policy="model",
    )

    with pytest.raises(ConfigError, match="a live model plays only in a stack run"):
        play_run(live_model, tmp_path / "run", mode="honest")

    assert not (tmp_path / "run").exists()
