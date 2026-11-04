"""What a run keeps of the forge after each play: each company repo's ``main``, graded, and its whole history.

After a play, ``read_forge`` has git archive each repo's ``main`` and bundle its every ref and commit where
the forge's repositories are: in a stack run inside the forge's container, so the host never runs git on a
repository agents push to; in an in-process run in this process, on the episode's own forge.
``write_played_forge`` unpacks the archives into the play's ``checkout``, which its main task is graded on:
only what is merged counts. An entry that would land outside it (an absolute path, a link leading out) is
skipped and logged, so a crafted tree cannot stop the run being graded. It writes the bundles, unopened, to
the play's ``forge_history``: every commit the play pushed or merged, a deleted branch's too
(``refs/archive``), which the records name.
"""

from __future__ import annotations

import functools
import io
import logging
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

from loc_arena.config import RunConfig
from loc_arena.forge_service import SERVICE as FORGE
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from meridian_forge.repositories import MAIN, repository_path

_LOGGER = logging.getLogger(__name__)
# The directories of a run's plays, as ``harness.play_run`` names them: the episode's, then its honest twin's.
EPISODE_PHASE, TWIN_PHASE = "episode", "honest_cal"
CHECKOUT, HISTORY = "checkout", "forge_history"  # in a play's directory: what is graded; each repo's bundle
BUNDLE_SUFFIX = ".bundle"

RunCompose = Callable[..., subprocess.CompletedProcess[bytes]]
RunGit = Callable[[list[str]], subprocess.CompletedProcess[bytes]]  # runs a git command where the forge is


class MergedMainError(RuntimeError):
    """The forge could not be read after a play, so the run cannot be graded on it."""


class PlayedForge(NamedTuple):
    """What a play left on the forge, by repo: its ``main`` as a tar, its every ref and commit as a bundle."""

    mains: dict[str, bytes]
    histories: dict[str, bytes]


def read_forge(repositories: Path, run_git: RunGit) -> PlayedForge:
    """What the play left on the forge whose repositories are in ``repositories``, read by ``run_git``."""
    mains, histories = {}, {}
    for repo in COMPANY_REPOS:
        git = ["git", "--git-dir", str(repository_path(repositories, repo))]
        for read, command, into in (
            ("archive", ["archive", "--format=tar", MAIN], mains),
            ("bundle", ["bundle", "create", "--quiet", "-", "--all"], histories),
        ):
            done = run_git([*git, *command])
            if done.returncode != 0:
                detail = done.stderr.decode().strip()
                raise MergedMainError(f"could not {read} {repo} on the forge: {detail}")
            into[repo] = done.stdout
    return PlayedForge(mains, histories)


def read_stack_forge(config: RunConfig, compose: list[str], run_compose: RunCompose) -> PlayedForge | None:
    """``read_forge`` in the forge's container of ``config``'s stack run; none when it has no forge."""
    if not any(service.name == FORGE for service in config.live_services):
        return None
    return read_forge(
        config.settings.forge.repositories,
        lambda command: run_compose([*compose, "exec", "-T", FORGE, *command], capture_output=True),
    )


def read_local_forge(repositories: Path) -> PlayedForge:
    """``read_forge`` of the forge whose repositories are in ``repositories`` here, in this process."""
    return read_forge(repositories, functools.partial(subprocess.run, capture_output=True, check=False))


def write_played_forge(play_directory: Path, played: PlayedForge) -> None:
    """Write ``played`` in ``play_directory``: each ``main`` in its checkout, and each history's bundle."""
    write_merged_checkout(play_directory / CHECKOUT, played.mains)
    history = play_directory / HISTORY
    history.mkdir(parents=True, exist_ok=True)
    for repo, bundle in played.histories.items():
        (history / f"{repo}{BUNDLE_SUFFIX}").write_bytes(bundle)


def write_merged_checkout(checkout: Path, archives: Mapping[str, bytes]) -> None:
    """Make ``checkout`` hold each repo of ``archives`` as archived, and nothing else."""
    shutil.rmtree(checkout, ignore_errors=True)
    for repo, archive in archives.items():
        destination = checkout / repo
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(destination, filter=keep_inside)


def write_played_forges(run_directory: Path, played: Mapping[str, PlayedForge]) -> None:
    """``write_played_forge`` of each play in ``run_directory``, by the play's directory name."""
    for phase, forge in played.items():
        write_played_forge(run_directory / phase, forge)


def keep_inside(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """Python's ``data`` filter, skipping an entry it refuses rather than refusing the whole archive."""
    try:
        return tarfile.data_filter(member, destination)
    except tarfile.FilterError as error:
        _LOGGER.warning("skipped %s of a merged main: %s", member.name, error)
        return None
