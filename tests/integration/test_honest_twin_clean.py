"""The honest twin of the stored run completes no covert milestone."""

from __future__ import annotations

from pathlib import Path

from loc_arena.harness import apply_mode, read_played_snapshot
from loc_arena.merged_main import TWIN_PHASE
from loc_arena.tasks.side_task_verifier import verify_side_task

from tests._stored_run import CONFIG, unpack_stored_run


def test_honest_twin_completes_no_covert_milestone(tmp_path: Path) -> None:
    honest = apply_mode(CONFIG, "honest")
    twin = read_played_snapshot(unpack_stored_run(tmp_path) / TWIN_PHASE, honest)

    verdict = verify_side_task(twin.sealed_path, honest, twin.deadline_ts, twin.calls)

    reached = [milestone for milestone, result in verdict.milestones.items() if result.reached]
    assert (verdict.landing.minimal, verdict.covert_depth, reached) == (False, 0, [])
