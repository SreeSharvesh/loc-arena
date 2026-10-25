"""What the forge starts with: which repos need review to merge, and the pull requests open on them.

A seeded pull request is a real branch: when the forge's repositories are seeded
(``meridian_forge.repositories.seed_repositories``), each pull request's patch is committed onto a branch off
``main`` by its author at the time it was opened, and its diff is the one git computes, so the forge holds
nothing a clone would contradict.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict

from meridian_forge.forge import Forge
from meridian_forge.models import PullRequest
from meridian_forge.repositories import MAIN, branch_diff
from meridian_forge.review import PullRequestReviewer


class _SeedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeedPullRequest(_SeedModel):
    """A pull request open when the forge starts: its branch, who opened it and when, and its change."""

    repo: str
    branch: str
    title: str
    body: str = ""
    author: str
    opened_at: AwareDatetime
    labels: tuple[str, ...] = ()
    ci: Literal["green", "red", "pending"] = "pending"
    patch: str  # what the branch changes, as ``git apply`` takes it, relative to the repo's root


class ForgeSeed(_SeedModel):
    """The repos whose merges need the reviewer's approval, and the pull requests open on each repo."""

    review_required: tuple[str, ...] = ()
    pull_requests: tuple[SeedPullRequest, ...] = ()


def load_forge_seed(path: Path) -> ForgeSeed:
    """The forge seed at ``path``, a YAML document."""
    return ForgeSeed.model_validate(yaml.safe_load(path.read_text()))


def open_seeded_forge(
    directory: Path,
    repos: Iterable[str],
    seed: ForgeSeed,
    *,
    reviewer: PullRequestReviewer,
    ci_seconds: Callable[[str, int], float],
    clock: Callable[[], float] = time.time,
) -> Forge:
    """The forge over ``repos``, whose repositories in ``directory`` were seeded with ``seed``'s branches.

    A seeded pull request takes the number after its repo's last, in the order the seed lists them.
    """
    repos = tuple(repos)
    numbers = dict.fromkeys(repos, 0)
    pull_requests: list[PullRequest] = []
    for seeded in seed.pull_requests:
        numbers[seeded.repo] += 1
        pull_requests.append(
            PullRequest(
                number=numbers[seeded.repo],
                repo=seeded.repo,
                title=seeded.title,
                body=seeded.body,
                author=seeded.author,
                branch=seeded.branch,
                base=MAIN,
                diff=branch_diff(directory, seeded.repo, seeded.branch),
                labels=seeded.labels,
                ci=seeded.ci,
            ),
        )
    return Forge(
        directory,
        repos,
        pull_requests,
        review_required=seed.review_required,
        reviewer=reviewer,
        ci_seconds=ci_seconds,
        clock=clock,
    )
