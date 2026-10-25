"""What a run grades: each company repo's ``main`` as merged on the forge, after each play.

After a play, ``archive_main`` has ``git archive`` each repo's ``main`` where the forge's repositories are: in
a stack run inside the forge's container, so the host never runs git on a repository agents push to; in an
in-process run in this process, on the episode's own forge. ``write_merged_checkout`` unpacks those archives
into the play's ``checkout``, which its main task is graded on: only what is merged counts. An entry that
would land outside it (an absolute path, a link leading out) is skipped and logged, so a crafted tree cannot
stop the run being graded.
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

from loc_arena.config import RunConfig
from loc_arena.forge_service import SERVICE as FORGE
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from meridian_forge.repositories import MAIN, repository_path

_LOGGER = logging.getLogger(__name__)
# The directories of a run's plays, as ``harness.play_run`` names them: the episode's, then its honest twin's.
EPISODE_PHASE, TWIN_PHASE = "episode", "honest_cal"

RunCompose = Callable[..., subprocess.CompletedProcess[bytes]]
RunGit = Callable[[list[str]], subprocess.CompletedProcess[bytes]]  # runs a git command where the forge is


class MergedMainError(RuntimeError):
    """The forge's ``main`` could not be read after a play, so the run cannot be graded on it."""


def archive_main(repositories: Path, run_git: RunGit) -> dict[str, bytes]:
    """Each company repo's ``main`` in the forge's ``repositories``, as a tar by repo, read by ``run_git``."""
    archives = {}
    for repo in COMPANY_REPOS:
        bare = repository_path(repositories, repo)
        done = run_git(["git", "--git-dir", str(bare), "archive", "--format=tar", MAIN])
        if done.returncode != 0:
            detail = done.stderr.decode().strip()
            raise MergedMainError(f"could not archive {repo}'s main on the forge: {detail}")
        archives[repo] = done.stdout
    return archives


def archive_stack_main(
    config: RunConfig,
    compose: list[str],
    run_compose: RunCompose,
) -> dict[str, bytes] | None:
    """``archive_main`` in the forge's container of ``config``'s stack run; none when it has no forge."""
    if not any(service.name == FORGE for service in config.live_services):
        return None
    return archive_main(
        config.settings.forge.repositories,
        lambda command: run_compose([*compose, "exec", "-T", FORGE, *command], capture_output=True),
    )


def archive_local_main(repositories: Path) -> dict[str, bytes]:
    """``archive_main`` of the forge whose repositories are in ``repositories`` here, in this process."""
    return archive_main(repositories, functools.partial(subprocess.run, capture_output=True, check=False))


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
