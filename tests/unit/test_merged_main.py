"""What a run grades: each repo's main as merged on the forge, written as the checkout."""

from __future__ import annotations

import dataclasses
import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_service import load_seed, open_forge
from loc_arena.iam_seed import REPOSITORY
from loc_arena.merged_main import MergedMainError, archive_stack_main, write_merged_checkout
from loc_arena.settings import ForgeSettings
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

from meridian_forge.repositories import commit_branch, identity, merge_branch

CFG = load_run_config("configs/aurora-efficiency.iam.yaml")
COMPOSE = ["docker", "compose", "-p", "loc-arena-test"]
REPO = "meridian-serving"


def config_with_forge_at(repositories: Path) -> Any:  # noqa: ANN401 - a RunConfig
    settings = CFG.settings.model_copy(update={"forge": ForgeSettings(repositories=repositories)})
    return dataclasses.replace(CFG, settings=settings)


def in_the_forge_container(command: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:  # noqa: ANN401
    """Compose's ``exec -T forge <command>``, run here, where this test keeps the forge's repositories."""
    assert command[: len(COMPOSE) + 3] == [*COMPOSE, "exec", "-T", "forge"]
    environment = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(command[len(COMPOSE) + 3 :], env=environment, check=False, **options)


def move_main(repositories: Path, path: str, content: str) -> None:
    """``REPO``'s main moved by the forge's merge of a branch adding ``path``."""
    patch = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{content}\n"
    commit_branch(repositories, REPO, "add-file", patch, "Add a file", identity("yuki"))
    assert merge_branch(repositories, REPO, "add-file", "Merge add-file", "omar")


def test_a_checkout_written_from_main_holds_main_and_nothing_the_episode_left(tmp_path: Path) -> None:
    repositories = tmp_path / "repositories"
    open_forge(CFG, load_seed(CFG), repositories)
    move_main(repositories, "MERGED.md", "merged by the team")
    checkout = tmp_path / "episode" / "checkout"
    (checkout / REPO).mkdir(parents=True)
    (checkout / REPO / "UNMERGED.md").write_text("an edit nobody merged\n")

    archives = archive_stack_main(config_with_forge_at(repositories), COMPOSE, in_the_forge_container)
    write_merged_checkout(checkout, archives or {})

    assert sorted(path.name for path in checkout.iterdir()) == sorted(COMPANY_REPOS)
    company = REPOSITORY / "company" / REPO
    assert (checkout / REPO / "pyproject.toml").read_text() == (company / "pyproject.toml").read_text()
    assert (checkout / REPO / "MERGED.md").read_text() == "merged by the team\n"
    assert not (checkout / REPO / "UNMERGED.md").exists()


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

    assert archive_stack_main(config, COMPOSE, in_the_forge_container) is None


def test_a_main_the_forge_cannot_archive_stops_the_run_from_being_graded(tmp_path: Path) -> None:
    with pytest.raises(MergedMainError, match="could not archive"):  # no repositories were seeded there
        archive_stack_main(config_with_forge_at(tmp_path / "empty"), COMPOSE, in_the_forge_container)
