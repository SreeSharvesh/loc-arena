"""What a stack run grades: each company repo's ``main`` as merged on the forge, after each play.

After a play, ``archive_main`` has ``git archive`` each repo's ``main`` inside the forge's container, so the
host never runs git on a repository agents push to. Before grading, ``write_merged_checkout`` unpacks those
archives into the play's ``checkout``, which its main task is graded on, in place of the checkout the episode
left: only what is merged counts. An entry that would land outside it (an absolute path, a link leading out)
is skipped and logged, so a crafted tree cannot stop the run being graded. A run whose scenario has no forge
grades the checkout the episode left.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.forge.repositories import MAIN, repository_path
from loc_arena.forge.service import SERVICE as FORGE
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

_LOGGER = logging.getLogger(__name__)
# The directories of a run's plays, as ``harness.play_run`` names them: the episode's, then its honest twin's.
EPISODE_PHASE, TWIN_PHASE = "episode", "honest_cal"

RunCompose = Callable[..., subprocess.CompletedProcess[bytes]]


class MergedMainError(RuntimeError):
    """The forge's ``main`` could not be read after a play, so the run cannot be graded on it."""


def archive_main(config: RunConfig, compose: list[str], run_compose: RunCompose) -> dict[str, bytes] | None:
    """Each company repo's ``main`` on ``config``'s forge, as a tar, by repo; none when it has no forge."""
    if not any(service.name == FORGE for service in config.live_services):
        return None
    archives = {}
    for repo in COMPANY_REPOS:
        bare = repository_path(config.settings.forge.repositories, repo)
        archive = ["git", "--git-dir", str(bare), "archive", "--format=tar", MAIN]
        done = run_compose([*compose, "exec", "-T", FORGE, *archive], capture_output=True)
        if done.returncode != 0:
            detail = done.stderr.decode().strip()
            raise MergedMainError(f"could not archive {repo}'s main on the forge: {detail}")
        archives[repo] = done.stdout
    return archives


def write_merged_checkout(checkout: Path, archives: Mapping[str, bytes]) -> None:
    """Make ``checkout`` hold each repo of ``archives`` as archived, and nothing else."""
    shutil.rmtree(checkout, ignore_errors=True)
    for repo, archive in archives.items():
        destination = checkout / repo
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(destination, filter=keep_inside)


def write_merged_checkouts(run_directory: Path, mains: Mapping[str, Mapping[str, bytes]]) -> None:
    """Make each play's ``checkout`` in ``run_directory`` hold what ``mains`` archived of it, by play."""
    for phase, archives in mains.items():
        write_merged_checkout(run_directory / phase / "checkout", archives)


def keep_inside(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """Python's ``data`` filter, skipping an entry it refuses rather than refusing the whole archive."""
    try:
        return tarfile.data_filter(member, destination)
    except tarfile.FilterError as error:
        _LOGGER.warning("skipped %s of a merged main: %s", member.name, error)
        return None
