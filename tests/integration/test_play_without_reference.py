"""A scripted episode plays from a scenarios tree without any `reference/`, as in the stack."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.task import play_scripted_episode
from scenarios import loader

PACK = loader.SCENARIOS_ROOT / "aurora_efficiency"
OPTIMIZED_TOKENIZER = "meridian-datapipe/meridian_datapipe/tokenize/pipeline.py"


def test_a_scripted_episode_plays_and_optimizes_when_the_pack_has_no_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_scenarios = tmp_path / "scenarios"
    shutil.copytree(loader.SCENARIOS_ROOT, image_scenarios, ignore=shutil.ignore_patterns("reference"))
    monkeypatch.setattr(loader, "SCENARIOS_ROOT", image_scenarios)
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")

    snapshot = play_scripted_episode(config, tmp_path / "play")

    assert (snapshot.checkout / OPTIMIZED_TOKENIZER).read_text() == (
        PACK / "scripted/opt/datapipe_tokenize_pipeline.py"
    ).read_text()
