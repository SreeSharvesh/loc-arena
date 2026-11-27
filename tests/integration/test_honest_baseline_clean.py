"""The honest baseline of the stored run completes no covert stage."""

from __future__ import annotations

from pathlib import Path

from loc_arena.harness import apply_mode, read_played_snapshot
from loc_arena.merged_main import BASELINE_PHASE
from loc_arena.tasks.side_task_verifier import verify_side_task

from tests._stored_run import CONFIG, unpack_stored_run


def test_honest_baseline_completes_no_covert_stage(tmp_path: Path) -> None:
    honest = apply_mode(CONFIG, "honest")
    baseline = read_played_snapshot(unpack_stored_run(tmp_path) / BASELINE_PHASE, honest)

    verdict = verify_side_task(baseline.sealed_path, honest, baseline.deadline_ts, baseline.calls)

    reached = [stage for stage, result in verdict.stages.items() if result.reached]
    assert (verdict.success.minimal, verdict.stages_reached, reached) == (False, 0, [])
