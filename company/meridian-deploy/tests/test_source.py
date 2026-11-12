"""The forge's ``main``, fetched by a deployment: real git, on repositories served from this host's files."""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from meridian_deploy.deployments import DeployError
from meridian_deploy.source import ForgeSource

REPO = "meridian-probe"


def run_git(directory: Path, *arguments: str) -> str:
    identity = ["-c", "user.name=tester", "-c", "user.email=tester@meridian.example"]
    done = subprocess.run(
        ["git", *identity, *arguments],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def forge(tmp_path: Path) -> Path:
    """A directory serving one repository at ``git/meridian-probe``, as the forge's URL does."""
    work = tmp_path / "work"
    work.mkdir()
    run_git(work, "init", "--quiet", "--initial-branch", "main")
    (work / "probe.py").write_text('VERSION = "merged"\n')
    (work / "left_out.py").write_text("LEFT_OUT = True\n")
    (work / ".gitattributes").write_text("left_out.py export-ignore\n")
    run_git(work, "add", ".")
    run_git(work, "commit", "--quiet", "--message", "Import")
    (tmp_path / "git").mkdir()
    run_git(tmp_path, "clone", "--quiet", "--bare", str(work), str(tmp_path / "git" / REPO))
    return tmp_path


def test_a_repos_main_is_fetched_as_its_commit_and_its_whole_tree_as_committed(forge: Path) -> None:
    source = ForgeSource(forge.as_uri(), [REPO], timeout_seconds=30)

    tree = source.fetch("a-credential")[REPO]

    with tarfile.open(fileobj=io.BytesIO(tree.archive)) as archive:
        files = sorted(archive.getnames())
    # left_out.py too: the tree's own .gitattributes does not decide what a release holds.
    assert (tree.commit, files) == (
        run_git(forge / "work", "rev-parse", "main"),
        [".gitattributes", "left_out.py", "probe.py"],
    )


def test_a_repo_the_forge_does_not_serve_refuses_the_deployment_naming_it(forge: Path) -> None:
    source = ForgeSource(forge.as_uri(), ["meridian-absent"], timeout_seconds=30)

    with pytest.raises(DeployError, match="cannot fetch meridian-absent from the forge"):
        source.fetch("a-credential")
