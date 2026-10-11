"""The model-driven star (policy: model) drives the full live assembler and stays clean when honest.

Drives ModelAgentPolicy-driven agent-main and its pre-provisioned sub-agents through a canned provider. The
covert tools are record-only services now (the rogue loop is a stub, #99 step 4), so a live run reaches no
loop-tied side-task milestone; the honest twin reaches none either, which this checks.
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.live import play_model_episode
from loc_arena.task import grade_snapshot

from tests.integration._live_support import QueuedProvider

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
DEADLINE = 10_000.0


def test_model_policy_honest_twin_still_clean(tmp_path: Path) -> None:
    # covert.enabled=false: the same machinery, no covert seed injected -> no rogue loop, verifies clean
    honest = apply_mode(CFG, "honest")
    ep = grade_snapshot(
        play_model_episode(honest, tmp_path / "h", provider=QueuedProvider({}), deadline_ts=DEADLINE),
        honest,
    )
    assert ep.verdict.landing.robust is False and ep.verdict.landing.minimal is False
    assert ep.verdict.covert_depth == 0
