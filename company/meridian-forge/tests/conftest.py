"""A forge hosting one repo, ``notes``, whose main holds ``NOTES.md`` with one line, ``first``."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from meridian_forge.forge import Forge
from meridian_forge.models import PullRequest
from meridian_forge.repositories import seed_repositories
from meridian_forge.review import ReviewDecision
from meridian_forge.seed import ForgeSeed, open_seeded_forge


class Approving:
    """A reviewer that approves every pull request."""

    def review(self, pr: PullRequest) -> ReviewDecision:
        return ReviewDecision(True, "fine", "grace")


@pytest.fixture
def repositories(tmp_path: Path) -> Path:
    return tmp_path / "repositories"


@pytest.fixture
def open_forge(tmp_path: Path, repositories: Path) -> Callable[[ForgeSeed], Forge]:
    """Open the forge over ``notes`` from a seed."""

    def open_with(seed: ForgeSeed) -> Forge:
        source = tmp_path / "source"
        source.mkdir(exist_ok=True)
        (source / "NOTES.md").write_text("first\n")
        seed_repositories(repositories, {"notes": source}, seed.pull_requests)
        return open_seeded_forge(
            repositories,
            ["notes"],
            seed,
            reviewer=Approving(),
            ci_seconds=lambda _repo, _number: 0.0,
            clock=time.time,
        )

    return open_with
