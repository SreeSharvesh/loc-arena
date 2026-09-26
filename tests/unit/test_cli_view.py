from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import load_run_config

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / CFG.run_slug / "run-x"
    bundle.mkdir(parents=True)
    (bundle / "report.html").write_text("")
    return bundle


def _view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setattr(cli, "load_run_config", lambda _path: CFG)
    assert cli.main(["view", "--out", str(tmp_path), "--no-open"]) == 0
    return capsys.readouterr().out


def test_view_prints_the_log_dir_command_for_a_real_inspect_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _bundle(tmp_path)
    with zipfile.ZipFile(bundle / "run-x.eval", "w") as archive:
        archive.writestr("header.json", "{}")
    assert f"inspect view --log-dir {bundle}" in _view(tmp_path, monkeypatch, capsys)


def test_view_prints_no_inspect_command_for_the_json_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "run-x.eval").write_text(json.dumps({"note": "placeholder"}))
    assert "inspect view" not in _view(tmp_path, monkeypatch, capsys)
