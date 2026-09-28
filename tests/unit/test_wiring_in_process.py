"""STACK=0 stays byte-identical: the in-process run writes the golden sealed log, mirror log and scores.

The goldens are what ``run_episode`` wrote for the deterministic config before the scaffold moved onto the
episode wiring (run instance id "golden"); only the wall-clock time a run took may differ.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import run_episode

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
GOLDEN = Path(__file__).parent / "golden" / "aurora-efficiency.deterministic"


@pytest.fixture(scope="module", params=["attack", "honest"])
def bundle(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    mode: str = request.param
    directory = run_episode(
        CONFIG,
        mode=mode,
        out_root=tmp_path_factory.mktemp(mode),
        run_instance_id="golden",
        write_report=False,
    )
    return mode, directory


def _scores_without_wall_clock(path: Path) -> dict[str, object]:
    scores = json.loads(path.read_text())
    del scores["wall_clock_seconds"]
    return scores


@pytest.mark.parametrize("log_name", ["events.sealed.jsonl", "events.mirror.jsonl"])
def test_the_in_process_run_writes_the_golden_log(bundle: tuple[str, Path], log_name: str) -> None:
    mode, directory = bundle

    written = (directory / log_name).read_bytes()

    assert written == (GOLDEN / mode / log_name).read_bytes()


def test_the_in_process_run_writes_the_golden_scores(bundle: tuple[str, Path]) -> None:
    mode, directory = bundle

    scores = _scores_without_wall_clock(directory / "scores.json")

    assert scores == _scores_without_wall_clock(GOLDEN / mode / "scores.json")
