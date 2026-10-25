"""The coworkers' forge changes are applied whole, or the world refuses to start."""

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_service import load_seed
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


def test_a_refused_coworker_merge_stops_the_world_from_starting() -> None:
    seed = load_seed(CFG)
    forge = Forge(
        ["meridian-serving", "meridian-controlplane"],
        [],
        review_required=seed.review_required,
        reviewer=_RefusingReviewer(),
        ci_seconds=lambda _repo, _number: 0.0,
    )

    with pytest.raises(WorldActivityError, match="blocked for the test"):
        apply_world_activity(forge, resolve_activity_repos(resolve_scenario(CFG), seed))
