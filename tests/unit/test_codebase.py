"""A scenario names the codebase its agents work on; every directory of a codebase is a repository."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.execution.checkout import list_codebase_repositories, list_repositories
from pydantic import ValidationError
from scenarios.loader import DEFAULT_CODEBASE, load_scenario


def _write_pack(scenarios_root: Path, name: str, codebase_line: str = "") -> None:
    (scenarios_root / name).mkdir(parents=True)
    (scenarios_root / name / "scenario.yaml").write_text(
        f"name: {name}\nscorer: some_scorer\nverifier: some_verifier\n{codebase_line}",
    )


def test_a_pack_naming_no_codebase_works_on_the_default_one(tmp_path: Path) -> None:
    _write_pack(tmp_path / "scenarios", "plain")

    scenario = load_scenario("plain", root=tmp_path / "scenarios")

    assert scenario.codebase == DEFAULT_CODEBASE


def test_a_pack_codebase_is_found_in_the_project_directory_of_its_scenarios(tmp_path: Path) -> None:
    _write_pack(tmp_path / "scenarios", "acme", "codebase: scenarios/acme/codebase\n")

    scenario = load_scenario("acme", root=tmp_path / "scenarios")

    assert scenario.codebase_directory == tmp_path / "scenarios" / "acme" / "codebase"


@pytest.mark.parametrize(
    "codebase",
    [
        "/etc",
        "../outside",
        "company/../configs",
        "./company",
        ".hidden",
        "Company",
        "company//repos",
        "'x y'",
        "''",
    ],
)
def test_a_codebase_outside_the_project_or_not_a_plain_path_is_refused(tmp_path: Path, codebase: str) -> None:
    _write_pack(tmp_path / "scenarios", "bad", f"codebase: {codebase}\n")

    with pytest.raises(ValidationError):
        load_scenario("bad", root=tmp_path / "scenarios")


def test_every_directory_of_a_codebase_but_hidden_ones_and_caches_is_a_repository(tmp_path: Path) -> None:
    for name in ("web", "acme-api", ".git", ".venv", "__pycache__"):
        (tmp_path / name).mkdir()
    (tmp_path / "README.md").write_text("")

    repositories = list_repositories(tmp_path)

    assert repositories == ("acme-api", "web")


def test_a_codebase_holding_no_repository_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    with pytest.raises(ValueError, match="holds no repository"):
        list_codebase_repositories(tmp_path)
