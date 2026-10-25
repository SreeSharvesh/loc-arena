"""The platform packages (meridian-iam, meridian-forge) sit beside the work repos, never among them."""

from pathlib import Path

import pytest
from loc_arena.tasks.main_task_grader import COMPANY_REPOS, seed_checkout

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("package", ["meridian-iam", "meridian-forge"])
def test_a_platform_package_is_never_in_the_agents_checkout(tmp_path: Path, package: str) -> None:
    assert (REPOSITORY / "company" / package).is_dir()
    assert package not in COMPANY_REPOS
    checkout = seed_checkout(tmp_path / "checkout")
    assert sorted(path.name for path in checkout.iterdir()) == list(COMPANY_REPOS)
    module = package.replace("-", "_")
    for repo in COMPANY_REPOS:  # and no work repo needs it to import: they are copied without it
        sources = list((checkout / repo).rglob("*.py"))
        assert not any(module in path.read_text() for path in sources), repo
