from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import RunConfig, load_run_config

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / CFG.run_slug / "run-x"
    bundle.mkdir(parents=True)
    (bundle / "report.html").write_text("")
    (bundle / "run-x.eval").write_text("")
    return bundle


def _view(
    tmp_path: Path, config: RunConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setattr(cli, "load_run_config", lambda _path: config)
    assert cli.main(["view", "--out", str(tmp_path), "--no-open"]) == 0
    return capsys.readouterr().out


def test_view_prints_the_log_dir_command_when_the_eval_is_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path)
    out = _view(tmp_path, dataclasses.replace(CFG, agent_transcript=True), monkeypatch, capsys)
    assert f"inspect view --log-dir {bundle}" in out


def test_view_prints_no_inspect_command_for_the_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bundle(tmp_path)
    assert "inspect view" not in _view(tmp_path, CFG, monkeypatch, capsys)
