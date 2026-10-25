"""The coworkers' forge changes are applied whole, or the world refuses to start."""

from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_service import load_seed, open_forge
from loc_arena.npcs.coworker import WorldActivityError, apply_world_activity, resolve_activity_repos
from loc_arena.npcs.reviewer import Reviewer
from loc_arena.task import resolve_scenario

from meridian_forge.forge import Forge
from meridian_forge.models import PullRequest
from meridian_forge.review import ReviewDecision

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


class _RefusingReviewer(Reviewer):
    """A reviewer that blocks every control-plane PR, the benign one included."""

    def review(self, pr: PullRequest) -> ReviewDecision:
        return ReviewDecision(approved=False, reason="blocked for the test", reviewer=self.name)


def test_a_refused_coworker_merge_stops_the_world_from_starting(tmp_path: Path) -> None:
    seed = load_seed(CFG)
    open_forge(CFG, seed, tmp_path)  # the forge's repositories, as a run seeds them
    forge = Forge(
        tmp_path,
        ["meridian-serving", "meridian-controlplane"],
        [],
        review_required=seed.review_required,
        reviewer=_RefusingReviewer(),
        ci_seconds=lambda _repo, _number: 0.0,
    )

    with pytest.raises(WorldActivityError, match="blocked for the test"):
        apply_world_activity(forge, tmp_path, resolve_activity_repos(resolve_scenario(CFG), seed))
