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
serves on and each repo's commit it released; ``read_deployed`` archives those commits, and
``write_played_forge`` says in ``deployed.json`` which deployment that is and which repos it leaves behind
``main``, with the trees in ``deployed_checkout`` when any is. The commits are the deploy service's word and
the trees the forge's: a deployed service's own files are never read, since its code can rewrite them. What
the play deployed is a second reading: when it cannot be made, ``deployed.json`` says why and ``main`` is
kept all the same.
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
    """The forge could not be read after a play: of ``main``, so the run cannot be graded on it."""


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

    deployment: int | None = None  # none when it could not be read
    service: str | None = None
    behind_main: list[str] = []  # the repos whose deployed commit is not the last of main
    error: str = ""  # why what the play deployed could not be read, when it could not


class Deployed(NamedTuple):
    """The last deployment of a play a service serves on, and each repo's tree at the commit it released."""

    deployment: ServingDeployment
    trees: dict[str, bytes]


class PlayedForge(NamedTuple):
    """What a play left on the forge, by repo: its ``main`` as a tar, its every ref and commit as a bundle."""

    mains: dict[str, bytes]
    histories: dict[str, bytes]
    deployed: Deployed | None = None  # none when the play deployed nothing a service serves on
    deployed_unread: str = ""  # why what the play deployed could not be read, when it could not


def find_last_deployment(record: str) -> ServingDeployment | None:
    """The last deployment a service serves on in the deploy service's ``record``; none when it has none.

    The last is the one of the highest number: the deploy service numbers them as they start, whatever order
    their calls end in.
    """
    serving: list[ServingDeployment] = []
    for line in record.splitlines():
        try:
            call = ToolRecord.model_validate_json(line)
            if call.tool == DEPLOY_TOOL:
                serving.append(ServingDeployment.model_validate(call.result))
        except ValidationError:
            continue  # a line that is no tool call, or a deployment that was refused or failed
    return max(serving, key=lambda deployment: deployment.number) if serving else None


def _read(run_git: RunGit, repositories: Path, repo: str, read: str, command: list[str]) -> bytes:
    """What git prints of ``command`` on ``repo`` of the forge; ``read`` names it when git fails."""
    done = run_git(["git", "--git-dir", str(repository_path(repositories, repo)), *command])
    if done.returncode != 0:
        detail = done.stderr.decode(errors="replace").strip()
        raise MergedMainError(f"could not {read} {repo} on the forge: {detail}")
    return done.stdout


def read_forge(repositories: Path, run_git: RunGit) -> PlayedForge:
    """What the play left on the forge whose repositories are in ``repositories``, read by ``run_git``."""
    mains, histories = {}, {}
    for repo in COMPANY_REPOS:
        mains[repo] = _read(run_git, repositories, repo, "archive", ["archive", "--format=tar", MAIN])
        bundle = ["bundle", "create", "--quiet", "-", "--all"]
        histories[repo] = _read(run_git, repositories, repo, "bundle", bundle)
    return PlayedForge(mains, histories)


def read_deployed(repositories: Path, run_git: RunGit, deployment: ServingDeployment) -> Deployed:
    """Each repo's tree at the commit ``deployment`` released, read as ``read_forge`` reads ``main``."""
    trees = {}
    for repo in COMPANY_REPOS:
        if repo not in deployment.commits:
            raise MergedMainError(f"deployment {deployment.number} names no commit of {repo}")
        archive = ["archive", "--format=tar", deployment.commits[repo]]
        trees[repo] = _read(run_git, repositories, repo, "archive what was deployed of", archive)
    return Deployed(deployment, trees)


def read_stack_forge(config: RunConfig, compose: list[str], run_compose: RunCompose) -> PlayedForge | None:
    """``read_forge`` in the forge's container of ``config``'s stack run; none when it has no forge.

    With a deploy service in the stack, what the play deployed too: the last deployment a service serves on
    in that service's log. A failure to read it is kept as ``deployed_unread``, never raised.
    """
    if not any(service.name == FORGE for service in config.live_services):
        return None

    def run_git(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        return run_compose([*compose, "exec", "-T", FORGE, *command], capture_output=True)

    repositories = config.settings.forge.repositories
    played = read_forge(repositories, run_git)
    deployer = find_deployer(config)
    if deployer is None:
        return played
    read_log = [*compose, "logs", "--no-color", "--no-log-prefix", deployer.name]
    logged = run_compose(read_log, capture_output=True)
    if logged.returncode != 0:
        return played._replace(deployed_unread=f"no record: {logged.stderr.decode(errors='replace').strip()}")
    deployment = find_last_deployment(logged.stdout.decode(errors="replace"))
    if deployment is None:
        return played
    try:
        return played._replace(deployed=read_deployed(repositories, run_git, deployment))
    except MergedMainError as error:
        return played._replace(deployed_unread=str(error))


def write_played_forge(play_directory: Path, played: PlayedForge) -> None:
    """Write ``played`` in ``play_directory``: each ``main`` in its checkout, and each history's bundle.

    What the play deployed is written beside them, in place of any the directory held: ``deployed.json``
    when it deployed or could not be read, and its trees in ``deployed_checkout`` when a repo is behind main.
    """
    write_merged_checkout(play_directory / CHECKOUT, played.mains)
    history = play_directory / HISTORY
    history.mkdir(parents=True, exist_ok=True)
    for repo, bundle in played.histories.items():
        (history / f"{repo}{BUNDLE_SUFFIX}").write_bytes(bundle)
    said = play_directory / DEPLOYED_FILE
    said.unlink(missing_ok=True)
    shutil.rmtree(play_directory / DEPLOYED_CHECKOUT, ignore_errors=True)
    if played.deployed_unread:
        said.write_text(DeployedFile(error=played.deployed_unread).model_dump_json())
    elif played.deployed:
        deployment, trees = played.deployed
        behind = sorted(repo for repo, tree in trees.items() if tree != played.mains[repo])
        level = DeployedFile(deployment=deployment.number, service=deployment.service, behind_main=behind)
        said.write_text(level.model_dump_json())
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
