"""The coworkers' forge changes are applied whole, or the world refuses to start."""

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_seed import generate_world
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
    scenario = resolve_scenario(CFG)
    world = generate_world(CFG, scenario, CFG.seed)
    forge = Forge(
        {name: repo.pull_requests for name, repo in world.repos.items()},
        control_plane_repos=["monitor-policy"],
        reviewer=_RefusingReviewer(),
        ci_seconds=lambda _repo, _number: 0.0,
    )

    with pytest.raises(WorldActivityError, match="blocked for the test"):
        apply_world_activity(forge, world, resolve_activity_repos(CFG, scenario))
