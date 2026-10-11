"""The coworkers' forge changes are applied whole, or the world refuses to start."""

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge.forge import Forge
from loc_arena.forge.reviewer import ReviewDecision, Reviewer
from loc_arena.forge.world import PullRequest, generate_world
from loc_arena.npcs.coworker import WorldActivityError, apply_world_activity, resolve_activity_repos
from loc_arena.task import resolve_scenario

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


class _RefusingReviewer(Reviewer):
    """A reviewer that blocks every control-plane PR, the benign one included."""

    def review(self, pr: PullRequest) -> ReviewDecision:
        return ReviewDecision(approved=False, reason="blocked for the test", reviewer=self.name)


def test_a_refused_coworker_merge_stops_the_world_from_starting() -> None:
    scenario = resolve_scenario(CFG)
    forge = Forge(generate_world(CFG, scenario, CFG.seed), reviewer=_RefusingReviewer())

    with pytest.raises(WorldActivityError, match="blocked for the test"):
        apply_world_activity(forge, resolve_activity_repos(CFG, scenario))
