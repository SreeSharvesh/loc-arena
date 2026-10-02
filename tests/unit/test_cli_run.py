"""``loc-arena run --stack`` when the harness fails: its message on stderr, a non-zero exit, no traceback."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena import cli, stack_episode

RUN = "aurora-efficiency.deterministic"


def _run_without_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(stack_episode, "docker_available", lambda _settings: False)
    return cli.main(["run", "--run", RUN, "--mode", "attack", "--stack", "--out", str(tmp_path)])


def test_a_stack_run_without_docker_exits_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = _run_without_docker(tmp_path, monkeypatch)

    assert code != 0


def test_a_stack_run_without_docker_names_the_failure_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_without_docker(tmp_path, monkeypatch)

    assert capsys.readouterr().err.startswith("error: Docker is not running")


def test_a_run_whose_config_is_missing_reports_it_in_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["run", "--run", "no-such-run"])

    assert capsys.readouterr().err == "error: config file not found: configs/no-such-run.yaml\n"
