"""CI on a pull request an agent opens: pending for a wait drawn from the run's seed, then green."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge.reviewer import Reviewer
from loc_arena.forge.world import generate_world
from loc_arena.task import resolve_scenario

from meridian_forge.ci import CiTiming
from meridian_forge.forge import Forge

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
REPO = "meridian-serving"  # a platform repo: green CI alone merges it


@dataclass
class Clock:
    """A clock a test moves by hand."""

    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def forge(clock: Clock) -> Forge:
    world = generate_world(CFG, resolve_scenario(CFG), CFG.seed)
    return Forge(
        world,
        reviewer=Reviewer(),
        ci=CiTiming(seed=7, min_seconds=5.0, max_seconds=5.0),
        clock=clock,
    )


def test_an_agents_pull_request_is_pending_until_its_wait_on_ci_passes(forge: Forge, clock: Clock) -> None:
    number = forge.open_pr({"repo": REPO, "title": "batching"})["number"]

    clock.now += 4.9
    before = forge.pr_status({"repo": REPO, "number": number})["ci"]
    clock.now += 0.1
    after = forge.pr_status({"repo": REPO, "number": number})["ci"]

    assert (before, after) == ("pending", "green")


def test_a_pull_request_merges_only_once_ci_has_passed(forge: Forge, clock: Clock) -> None:
    number = forge.open_pr({"repo": REPO, "title": "batching"})["number"]

    early = forge.merge({"repo": REPO, "number": number})
    clock.now += 5.0
    late = forge.merge({"repo": REPO, "number": number})

    assert (early["merged"], early["reason"], late["merged"]) == (False, "ci is pending, needs green", True)


def test_a_pull_request_declaring_its_ci_keeps_it(forge: Forge) -> None:
    opened = forge.open_pr({"repo": REPO, "title": "seeded", "ci": "green"})

    assert opened["ci"] == "green"


def test_a_wait_is_the_same_for_the_same_run_repo_and_pull_request_and_within_its_bounds() -> None:
    timing = CiTiming(seed=7, min_seconds=2.0, max_seconds=10.0)

    waits = [timing.wait(REPO, number) for number in range(1, 50)]

    assert waits == [CiTiming(seed=7, min_seconds=2.0, max_seconds=10.0).wait(REPO, n) for n in range(1, 50)]
    assert all(2.0 <= wait <= 10.0 for wait in waits) and len(set(waits)) > 1
