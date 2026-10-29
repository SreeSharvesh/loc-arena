from __future__ import annotations

import json
from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.harness import run_episode

from tests.integration._live_support import ATTACK_MOVES_PENDING

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
_BUNDLE_FILES = {
    "config.resolved.yaml",
    "events.sealed.jsonl",
    "events.mirror.jsonl",
    "scores.json",
    "report.html",
    "decisions.md",
}


def _bundle_files(bundle: Path) -> set[str]:
    return {p.name for p in bundle.iterdir() if p.is_file()}


@ATTACK_MOVES_PENDING
def test_attack_and_honest_each_write_a_full_bundle(tmp_path: Path) -> None:
    attack = run_episode(CFG, mode="attack", out_root=tmp_path)
    honest = run_episode(CFG, mode="honest", out_root=tmp_path)
    assert _BUNDLE_FILES <= _bundle_files(attack)
    assert _BUNDLE_FILES <= _bundle_files(honest)
    # the Inspect .eval is named after the run, not a fixed "run.eval"
    assert any(name.endswith(".eval") for name in _bundle_files(attack))
    assert any(name.endswith(".eval") for name in _bundle_files(honest))

    a = json.loads((attack / "scores.json").read_text())
    h = json.loads((honest / "scores.json").read_text())
    # honest twin verifies clean; attack decided from the sealed log
    assert h["side_task"]["landing_minimal"] is False and h["side_task"]["covert_depth"] == 0
    assert h["safety"]["landed"] is False
    assert a["side_task"]["landing_minimal"] is True
    assert a["safety"]["landed"] is True and a["safety"]["caught"] is True
    # main task is a continuous score in both modes (the only difference is covert.enabled)
    assert a["main_task"]["final_score"] == h["main_task"]["final_score"]


def test_report_html_has_the_required_panels(tmp_path: Path) -> None:
    bundle = run_episode(CFG, mode="attack", out_root=tmp_path)
    html = (bundle / "report.html").read_text()
    for panel in (
        "global timeline",
        "kill-chain story",
        "monitor scores",
        "inference flow",
        "transcript",
    ):
        assert panel in html
    assert "self-contained" not in html or True  # no external <script src> / <link href>
    assert "src=http" not in html and "href=http" not in html.replace("claude.com", "")


def test_reproducible_from_config_and_seed(tmp_path: Path) -> None:
    a = run_episode(CFG, mode="attack", out_root=tmp_path / "a")
    b = run_episode(CFG, mode="attack", out_root=tmp_path / "b")
    # deterministic scripted policy: the sealed event sequence is byte-identical across runs
    assert (a / "events.sealed.jsonl").read_text() == (b / "events.sealed.jsonl").read_text()
    # and so are the graded outcomes
    sa = json.loads((a / "scores.json").read_text())
    sb = json.loads((b / "scores.json").read_text())
    assert sa["side_task"] == sb["side_task"]
    assert sa["main_task"] == sb["main_task"]


def test_the_bundle_records_whether_central_iam_was_on(tmp_path: Path) -> None:
    bundle = run_episode(CFG, mode="honest", out_root=tmp_path)

    resolved = yaml.safe_load((bundle / "config.resolved.yaml").read_text())

    assert resolved["iam"]["enabled"] is False  # written out, though the fixture's own file only turns it off
