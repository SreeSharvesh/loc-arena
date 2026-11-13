"""What a run keeps of the forge after each play: each company repo's ``main``, graded, and its whole history.

After a play, ``read_forge`` has git archive each repo's ``main`` and bundle its every ref and commit where
the forge's repositories are, inside the forge's container, so the host never runs git on a repository
agents push to.
``write_played_forge`` unpacks the archives into the play's ``checkout``, which its main task is graded on:
only what is merged counts. An entry that would land outside it (an absolute path, a link leading out) is
skipped and logged, so a crafted tree cannot stop the run being graded. It writes the bundles, unopened, to
the play's ``forge_history``: every commit the play pushed or merged, a deleted branch's too
(``refs/archive``), which the records name.

A play that deployed leaves a second tree. The deploy service's record names the last deployment a service
serves on and each repo's commit it released; ``read_forge`` archives those commits too, and
``write_played_forge`` says in ``deployed.json`` which deployment that is and which repos it leaves behind
``main``, with the trees in ``deployed_checkout`` when any is. The commits are the deploy service's word and
the trees the forge's: a deployed service's own files are never read, since its code can rewrite them.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loc_arena.config import RunConfig
from loc_arena.deploy_pool import find_deployer
from loc_arena.forge_service import SERVICE as FORGE
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from loc_arena.tool_records import ToolRecord
from meridian_forge.repositories import MAIN, repository_path

_LOGGER = logging.getLogger(__name__)
# The directories of a run's plays, as ``harness.play_run`` names them: the episode's, then its honest twin's.
EPISODE_PHASE, TWIN_PHASE = "episode", "honest_cal"
CHECKOUT, HISTORY = "checkout", "forge_history"  # in a play's directory: what is graded; each repo's bundle
DEPLOYED_CHECKOUT, DEPLOYED_FILE = (
    "deployed_checkout",
    "deployed.json",
)  # what the play's last deployment released
BUNDLE_SUFFIX = ".bundle"
DEPLOY_TOOL = "deploy"

RunCompose = Callable[..., subprocess.CompletedProcess[bytes]]
RunGit = Callable[[list[str]], subprocess.CompletedProcess[bytes]]  # runs a git command where the forge is


class MergedMainError(RuntimeError):
    """The forge could not be read after a play, so the run cannot be graded on it."""


class ServingDeployment(BaseModel):
    """A deployment its service serves on, as the deploy service's record holds its reply."""

    model_config = ConfigDict(frozen=True)

    number: int
    service: str
    commits: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]]  # each repo's commit of main then
    state: Literal["serving"]


class DeployedFile(BaseModel):
    """A play's ``deployed.json``: its last deployment a service serves on, and the repos it leaves behind."""

    model_config = ConfigDict(frozen=True)

    deployment: int
    service: str
    behind_main: list[str]  # the repos whose deployed commit is not the last of main


class Deployed(NamedTuple):
    """The last deployment of a play a service serves on, and each repo's tree at the commit it released."""

    deployment: ServingDeployment
    trees: dict[str, bytes]


class PlayedForge(NamedTuple):
    """What a play left on the forge, by repo: its ``main`` as a tar, its every ref and commit as a bundle."""

    mains: dict[str, bytes]
    histories: dict[str, bytes]
    deployed: Deployed | None = None  # none when the play deployed nothing a service serves on


def find_last_deployment(record: str) -> ServingDeployment | None:
    """The last deployment a service serves on in the deploy service's ``record``; none when it has none."""
    serving = None
    for line in record.splitlines():
        try:
            call = ToolRecord.model_validate_json(line)
            if call.tool == DEPLOY_TOOL:
                serving = ServingDeployment.model_validate(call.result)
        except ValidationError:
            continue  # a line that is no tool call, or a deployment that was refused or failed
    return serving


def read_forge(
    repositories: Path,
    run_git: RunGit,
    deployment: ServingDeployment | None = None,
) -> PlayedForge:
    """What the play left on the forge whose repositories are in ``repositories``, read by ``run_git``.

    With a ``deployment``, each repo's tree at the commit it released too.
    """
    mains, histories, trees = {}, {}, {}
    for repo in COMPANY_REPOS:
        git = ["git", "--git-dir", str(repository_path(repositories, repo))]
        reads = [
            ("archive", ["archive", "--format=tar", MAIN], mains),
            ("bundle", ["bundle", "create", "--quiet", "-", "--all"], histories),
        ]
        if deployment:
            if repo not in deployment.commits:
                raise MergedMainError(f"deployment {deployment.number} names no commit of {repo}")
            reads.append(("archive", ["archive", "--format=tar", deployment.commits[repo]], trees))
        for read, command, into in reads:
            done = run_git([*git, *command])
            if done.returncode != 0:
                detail = done.stderr.decode().strip()
                raise MergedMainError(f"could not {read} {repo} on the forge: {detail}")
            into[repo] = done.stdout
    return PlayedForge(mains, histories, Deployed(deployment, trees) if deployment else None)


def read_stack_forge(config: RunConfig, compose: list[str], run_compose: RunCompose) -> PlayedForge | None:
    """``read_forge`` in the forge's container of ``config``'s stack run; none when it has no forge.

    The deployment is the last a service serves on in the log of the stack's deploy service, when it has one.
    """
    if not any(service.name == FORGE for service in config.live_services):
        return None
    deployer, deployment = find_deployer(config), None
    if deployer:
        logged = run_compose(
            [*compose, "logs", "--no-color", "--no-log-prefix", deployer.name],
            capture_output=True,
        )
        if logged.returncode != 0:
            raise MergedMainError(
                f"could not read the deploy service's record: {logged.stderr.decode().strip()}",
            )
        deployment = find_last_deployment(logged.stdout.decode(errors="replace"))
    return read_forge(
        config.settings.forge.repositories,
        lambda command: run_compose([*compose, "exec", "-T", FORGE, *command], capture_output=True),
        deployment,
    )


def write_played_forge(play_directory: Path, played: PlayedForge) -> None:
    """Write ``played`` in ``play_directory``: each ``main`` in its checkout, and each history's bundle."""
    write_merged_checkout(play_directory / CHECKOUT, played.mains)
    history = play_directory / HISTORY
    history.mkdir(parents=True, exist_ok=True)
    for repo, bundle in played.histories.items():
        (history / f"{repo}{BUNDLE_SUFFIX}").write_bytes(bundle)
    if played.deployed:
        deployment, trees = played.deployed
        behind = sorted(repo for repo, tree in trees.items() if tree != played.mains[repo])
        said = DeployedFile(deployment=deployment.number, service=deployment.service, behind_main=behind)
        (play_directory / DEPLOYED_FILE).write_text(said.model_dump_json())
        if behind:  # the same trees as main's are graded once, as main's
            write_merged_checkout(play_directory / DEPLOYED_CHECKOUT, trees)


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
