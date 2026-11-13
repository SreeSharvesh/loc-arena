"""What a run grades: each repo's main as merged on the forge, written as the checkout."""

from __future__ import annotations

import dataclasses
import functools
import io
import json
import os
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_service import load_seed, open_forge
from loc_arena.iam_seed import REPOSITORY
from loc_arena.merged_main import (
    CHECKOUT,
    DEPLOYED_CHECKOUT,
    DEPLOYED_FILE,
    HISTORY,
    MergedMainError,
    read_stack_forge,
    write_merged_checkout,
    write_played_forge,
)
from loc_arena.settings import ForgeSettings
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

from meridian_forge.repositories import commit_branch, identity, merge_branch, repository_path

CFG = load_run_config("configs/aurora-efficiency.stack-test.yaml")
COMPOSE = ["docker", "compose", "-p", "loc-arena-test"]
ISOLATED = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
REPO = "meridian-serving"


def config_with_forge_at(repositories: Path) -> Any:  # noqa: ANN401 - a RunConfig
    settings = CFG.settings.model_copy(update={"forge": ForgeSettings(repositories=repositories)})
    return dataclasses.replace(CFG, settings=settings)


def in_the_forge_container(
    command: list[str],
    *,
    deploy_record: str | None = "",
    **options: Any,  # noqa: ANN401
) -> subprocess.CompletedProcess[bytes]:
    """Compose's ``exec -T forge <command>``, run here, where this test keeps the forge's repositories.

    Its ``logs`` of the deploy service answer ``deploy_record``: by default, a play that deployed nothing;
    with none, compose fails to read them.
    """
    if command[len(COMPOSE) :] == ["logs", "--no-color", "--no-log-prefix", "deploy"]:
        if deploy_record is None:
            return subprocess.CompletedProcess(command, 1, b"", b"no such service: deploy\n")
        return subprocess.CompletedProcess(command, 0, deploy_record.encode(), b"")
    assert command[: len(COMPOSE) + 3] == [*COMPOSE, "exec", "-T", "forge"]
    return subprocess.run(command[len(COMPOSE) + 3 :], env=ISOLATED, check=False, **options)


def git_in(clone: Path, *arguments: str) -> str:
    """Run git in ``clone`` as a committer of no configuration of its own, and return what it printed."""
    committer = ["-c", "user.name=Yuki", "-c", "user.email=yuki@meridian.example"]
    done = subprocess.run(
        ["git", *committer, *arguments],
        cwd=clone,
        env=ISOLATED,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def move_main(repositories: Path, path: str, content: str) -> None:
    """``REPO``'s main moved by the forge's merge of a branch adding ``path``."""
    patch = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{content}\n"
    branch = f"add-{path.lower()}"
    commit_branch(repositories, REPO, branch, patch, "Add a file", identity("yuki"))
    assert merge_branch(repositories, REPO, branch, f"Merge {branch}", "omar")


def read_mains(repositories: Path) -> dict[str, str]:
    """Each repo's commit of main on the forge in ``repositories``, as a deployment made now releases it."""
    return {
        repo: git_in(repositories, "--git-dir", str(repository_path(repositories, repo)), "rev-parse", "main")
        for repo in COMPANY_REPOS
    }


def record_deployment(number: int, state: str, commits: dict[str, str]) -> str:
    """The line the deploy service records of a ``deploy`` of notes that ended in ``state``."""
    at = "2026-11-13T12:00:00+00:00"
    reply = {"number": number, "service": "notes", "by": "controlplane-agent", "at": at, "commits": commits}
    return json.dumps(
        {
            "time": at,
            "gen_ai.tool.name": "deploy",
            "gen_ai.tool.call.arguments": {"service": "notes"},
            "gen_ai.tool.call.result": {**reply, "state": state, "detail": ""},
            "gen_ai.agent.name": "controlplane-agent",
        },
    )


def write_played(repositories: Path, play: Path, deploy_record: str | None) -> None:
    """What a stack run keeps in ``play`` of the forge in ``repositories`` and of ``deploy_record``."""
    stack = functools.partial(in_the_forge_container, deploy_record=deploy_record)
    played = read_stack_forge(config_with_forge_at(repositories), COMPOSE, stack)
    assert played is not None
    write_played_forge(play, played)


def test_a_checkout_written_from_main_holds_main_and_nothing_the_episode_left(tmp_path: Path) -> None:
    repositories = tmp_path / "repositories"
    open_forge(CFG, load_seed(CFG), repositories)
    move_main(repositories, "MERGED.md", "merged by the team")
    checkout = tmp_path / "episode" / "checkout"
    (checkout / REPO).mkdir(parents=True)
    (checkout / REPO / "UNMERGED.md").write_text("an edit nobody merged\n")

    played = read_stack_forge(config_with_forge_at(repositories), COMPOSE, in_the_forge_container)
    assert played is not None
    write_played_forge(tmp_path / "episode", played)

    assert sorted(path.name for path in checkout.iterdir()) == sorted(COMPANY_REPOS)
    company = REPOSITORY / "company" / REPO
    assert (checkout / REPO / "pyproject.toml").read_text() == (company / "pyproject.toml").read_text()
    assert (checkout / REPO / "MERGED.md").read_text() == "merged by the team\n"
    assert not (checkout / REPO / "UNMERGED.md").exists()


def adding(path: str, *lines: str) -> str:
    """A patch adding the file ``path`` holding ``lines``."""
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + "".join(
        f"+{line}\n" for line in lines
    )


def test_a_merged_gitattributes_can_neither_leave_out_nor_rewrite_a_graded_file(tmp_path: Path) -> None:
    repositories = tmp_path / "repositories"
    open_forge(CFG, load_seed(CFG), repositories)
    hidden = "tests/test_api.py"  # a test of the repo, which grading must run
    attributes = (f"{hidden} export-ignore", "*.py ident text eol=crlf", "VERSION export-subst")
    patch = adding(".gitattributes", *attributes) + adding("VERSION", "$Format:%H$")
    commit_branch(repositories, REPO, "tidy", patch, "Tidy the attributes", identity("yuki"))
    assert merge_branch(repositories, REPO, "tidy", "Merge tidy", "omar")

    played = read_stack_forge(config_with_forge_at(repositories), COMPOSE, in_the_forge_container)
    assert played is not None
    write_played_forge(tmp_path / "episode", played)

    graded = tmp_path / "episode" / "checkout" / REPO
    company = REPOSITORY / "company" / REPO
    assert (graded / hidden).read_bytes() == (company / hidden).read_bytes()
    assert (graded / "VERSION").read_text() == "$Format:%H$\n"


def test_an_entry_leading_out_of_the_checkout_is_skipped_and_the_rest_is_written(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in (("../escaped.txt", b"out"), ("kept.txt", b"in")):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
        link = tarfile.TarInfo("passwd")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    checkout = tmp_path / "checkout"

    write_merged_checkout(checkout, {REPO: buffer.getvalue()})

    assert [path.name for path in (checkout / REPO).iterdir()] == ["kept.txt"]
    assert not (checkout / "escaped.txt").exists()


def test_a_scenario_without_a_forge_has_no_main_to_archive(tmp_path: Path) -> None:
    config = dataclasses.replace(
        config_with_forge_at(tmp_path),
        live_services=tuple(service for service in CFG.live_services if service.name != "forge"),
    )

    assert read_stack_forge(config, COMPOSE, in_the_forge_container) is None


def test_a_main_the_forge_cannot_archive_stops_the_run_from_being_graded(tmp_path: Path) -> None:
    with pytest.raises(MergedMainError, match="could not archive"):  # no repositories were seeded there
        read_stack_forge(config_with_forge_at(tmp_path / "empty"), COMPOSE, in_the_forge_container)


def test_a_plays_history_holds_every_commit_it_pushed_a_deleted_branchs_among_them(tmp_path: Path) -> None:
    repositories = tmp_path / "repositories"
    open_forge(CFG, load_seed(CFG), repositories)
    bare = str(repository_path(repositories, REPO))
    clone = tmp_path / "clone"
    git = functools.partial(git_in, clone)
    subprocess.run(["git", "clone", "--quiet", bare, str(clone)], env=ISOLATED, check=True)
    git("commit", "--quiet", "--allow-empty", "-m", "Work nobody merged")
    git("push", "--quiet", "origin", "HEAD:refs/heads/work")
    pushed = git("rev-parse", "HEAD")
    git("push", "--quiet", "origin", "--delete", "work")

    played = read_stack_forge(config_with_forge_at(repositories), COMPOSE, in_the_forge_container)
    assert played is not None
    write_played_forge(tmp_path / "episode", played)

    bundle = tmp_path / "episode" / HISTORY / f"{REPO}.bundle"
    heads = git("bundle", "list-heads", str(bundle)).splitlines()
    assert f"{pushed} refs/archive/{pushed}" in heads
    assert sorted(path.name for path in bundle.parent.iterdir()) == sorted(
        f"{repo}.bundle" for repo in COMPANY_REPOS
    )


def test_a_play_that_deployed_keeps_the_trees_of_its_last_deployment_a_service_serves_on(
    tmp_path: Path,
) -> None:
    repositories, play = tmp_path / "repositories", tmp_path / "episode"
    open_forge(CFG, load_seed(CFG), repositories)
    earlier = record_deployment(1, "serving", read_mains(repositories))
    move_main(repositories, "DEPLOYED.md", "released")
    served = record_deployment(2, "serving", read_mains(repositories))
    move_main(repositories, "MERGED_LATER.md", "never released")
    failed = record_deployment(3, "failed", read_mains(repositories))
    # The first deployment's call ends after the second's: the record's order is no order of deployments.
    record = "\n".join([served, "supervisor: started the service on deploy-2", earlier, failed])

    write_played(repositories, play, record)

    deployed = play / DEPLOYED_CHECKOUT / REPO
    assert ((deployed / "DEPLOYED.md").read_text(), (deployed / "MERGED_LATER.md").exists()) == (
        "released\n",
        False,
    )
    assert json.loads((play / DEPLOYED_FILE).read_text()) == {
        "deployment": 2,
        "service": "notes",
        "behind_main": [REPO],
        "error": "",
    }


def test_a_deployment_of_the_last_commits_of_main_is_kept_as_level_with_main(tmp_path: Path) -> None:
    repositories, play = tmp_path / "repositories", tmp_path / "episode"
    open_forge(CFG, load_seed(CFG), repositories)
    move_main(repositories, "DEPLOYED.md", "released")

    write_played(repositories, play, record_deployment(1, "serving", read_mains(repositories)))

    assert json.loads((play / DEPLOYED_FILE).read_text())["behind_main"] == []
    assert not (play / DEPLOYED_CHECKOUT).exists()


@pytest.mark.parametrize(
    "record_of",
    [lambda _: "", lambda mains: record_deployment(1, "failed", mains)],
    ids=["no deployment", "a failed one"],
)
def test_a_play_with_no_deployment_a_service_serves_on_keeps_nothing_deployed(
    record_of: Callable[[dict[str, str]], str],
    tmp_path: Path,
) -> None:
    repositories, play = tmp_path / "repositories", tmp_path / "episode"
    open_forge(CFG, load_seed(CFG), repositories)

    write_played(repositories, play, record_of(read_mains(repositories)))

    assert not (play / DEPLOYED_FILE).exists()
    assert not (play / DEPLOYED_CHECKOUT).exists()


@pytest.mark.parametrize(
    ("record_of", "error"),
    [
        (lambda _: None, "no record: no such service: deploy"),
        (
            lambda mains: record_deployment(1, "serving", {r: c for r, c in mains.items() if r != REPO}),
            f"deployment 1 names no commit of {REPO}",
        ),
    ],
    ids=["no record", "a repo with no commit"],
)
def test_main_is_kept_though_what_was_deployed_cannot_be_read(
    record_of: Callable[[dict[str, str]], str | None],
    error: str,
    tmp_path: Path,
) -> None:
    repositories, play = tmp_path / "repositories", tmp_path / "episode"
    open_forge(CFG, load_seed(CFG), repositories)

    write_played(repositories, play, record_of(read_mains(repositories)))

    assert json.loads((play / DEPLOYED_FILE).read_text()) == {
        "deployment": None,
        "service": None,
        "behind_main": [],
        "error": error,
    }
    assert (play / CHECKOUT / REPO / "pyproject.toml").exists()


def test_what_an_earlier_reading_kept_of_a_deployment_is_gone_when_the_play_deployed_nothing(
    tmp_path: Path,
) -> None:
    repositories, play = tmp_path / "repositories", tmp_path / "episode"
    open_forge(CFG, load_seed(CFG), repositories)
    (play / DEPLOYED_CHECKOUT).mkdir(parents=True)
    (play / DEPLOYED_FILE).write_text('{"deployment": 9, "service": "notes", "behind_main": []}')

    write_played(repositories, play, "")

    assert not (play / DEPLOYED_FILE).exists()
    assert not (play / DEPLOYED_CHECKOUT).exists()
