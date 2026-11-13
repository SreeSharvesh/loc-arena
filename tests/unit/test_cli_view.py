from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import load_run_config

CFG = load_run_config("configs/aurora-efficiency.yaml")


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


def _graded_bundle(tmp_path: Path, name: str, written: float) -> Path:
    """A bundle with a report and scores, its report last written at ``written``."""
    bundle = tmp_path / CFG.run_slug / name
    bundle.mkdir(parents=True)
    (bundle / "report.html").write_text("")
    os.utime(bundle / "report.html", (written, written))
    scores = {
        "run_name": name,
        "mode": "attack",
        "main_task": {"final_score": 1.0, "gate_reason": "gate_passed"},
        "side_task": {"landing_minimal": False, "covert_depth": 0},
        "safety": {"landed": False, "caught": False, "caught_by": []},
    }
    (bundle / "scores.json").write_text(json.dumps(scores))
    return bundle


def test_view_names_the_report_written_last_whatever_the_bundles_are_called(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _graded_bundle(tmp_path, "zz-first", written=1_000.0)
    latest = _graded_bundle(tmp_path, "aa-second", written=2_000.0)

    printed = _view(tmp_path, monkeypatch, capsys)

    assert f"report: {latest / 'report.html'}" in printed


def test_view_opens_the_latest_report_not_the_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    latest = _graded_bundle(tmp_path, "run-x", written=2_000.0)
    opened: list[str] = []
    monkeypatch.setattr(cli, "load_run_config", lambda _path: CFG)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    cli.main(["view", "--out", str(tmp_path)])

    assert opened == [(latest / "report.html").resolve().as_uri()]


def test_the_run_index_lists_runs_newest_first_with_the_time_each_was_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _graded_bundle(tmp_path, "zz-first", written=1_000.0)
    _graded_bundle(tmp_path, "aa-second", written=2_000.0)

    _view(tmp_path, monkeypatch, capsys)

    index = (tmp_path / "index.html").read_text()
    assert index.index("aa-second") < index.index("zz-first")
    assert "1970-01-01T00:33:20+00:00" in index


def test_a_copied_run_keeps_its_place_by_when_it_finished_playing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    played_last = _graded_bundle(tmp_path, "aa-played-last", written=1_000.0)  # copied first: an older file
    played_first = _graded_bundle(tmp_path, "zz-played-first", written=2_000.0)
    for bundle, finished in ((played_last, 9_000.0), (played_first, 8_000.0)):
        snapshot = {
            "sealed_path": "s",
            "mirror_path": "m",
            "checkout": "c",
            "deadline_ts": 1.0,
            "mode": "attack",
        }
        snapshot["finished_at"] = finished
        (bundle / "episode").mkdir()
        (bundle / "episode" / "snapshot.json").write_text(json.dumps(snapshot))

    printed = _view(tmp_path, monkeypatch, capsys)

    assert f"report: {played_last / 'report.html'}" in printed


def test_a_run_whose_snapshot_holds_no_real_time_is_placed_by_its_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    damaged = _graded_bundle(tmp_path, "run-x", written=2_000.0)
    snapshot = {"sealed_path": "s", "mirror_path": "m", "checkout": "c", "deadline_ts": 1.0, "mode": "attack"}
    (damaged / "episode").mkdir()
    (damaged / "episode" / "snapshot.json").write_text(json.dumps({**snapshot, "finished_at": "nan"}))

    _view(tmp_path, monkeypatch, capsys)

    assert "1970-01-01T00:33:20+00:00" in (tmp_path / "index.html").read_text()
