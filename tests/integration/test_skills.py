"""The scenario's skills: each script, run from the workspace root reports what the tools did.

Expected results are what the harness's own run_tests and run_benchmark tools returned on a pristine checkout
before they became skills; pytest's duration is dropped from the summary. In process the harness still offers
both tools, which run the same scripts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.tools import StubServices
from loc_arena.settings import StackSettings
from loc_arena.tasks.main_task_grader import _REPOS, seed_checkout
from scenarios.loader import SCENARIOS_ROOT

SKILLS = SCENARIOS_ROOT / "aurora_efficiency" / "skills"
RUN_TESTS = "run-tests/scripts/run_tests.py"
RUN_BENCHMARK = "run-benchmark/scripts/run_benchmark.py"
GREEN_COMMON = {"repo": "meridian-common", "passed": True, "returncode": 0, "summary": "72 passed, 2 xfailed"}
PRISTINE_COST = {"cost": 346, "by_kind": {"datapipe.dedup_compare": 148, "datapipe.tokens": 198}}
UNKNOWN_REPO = {
    "error": "unknown repo 'meridian-nowhere'; choices: ['meridian-common', 'meridian-controlplane', "
    "'meridian-datapipe', 'meridian-distill', 'meridian-evalkit', 'meridian-jobsvc', 'meridian-serving']",
}
# The Agent Skills specification's rule for a skill's name: https://agentskills.io/specification
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return seed_checkout(tmp_path_factory.mktemp("pristine") / "checkout")


def run_skill(checkout: Path, script: str, *arguments: str) -> dict[str, Any]:
    """What ``script`` of the skills prints last, run from ``checkout`` with no PYTHONPATH of its own."""
    ran = subprocess.run(
        [sys.executable, str(SKILLS / script), *arguments],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
    )
    return json.loads(ran.stdout.strip().splitlines()[-1])


def serve_in_process(checkout: Path) -> CodeServices:
    """The code tools of an in-process run over ``checkout``, with the scenario's skills."""
    return CodeServices(
        StubServices(),
        checkout=checkout,
        repos=list(_REPOS),
        stack=StackSettings(),
        skills=SKILLS,
    )


def read_front_matter(skill: str) -> dict[str, Any]:
    """The YAML between the two ``---`` lines that open ``skill``'s SKILL.md."""
    _, front, _ = (SKILLS / skill / "SKILL.md").read_text().split("---\n", 2)
    return yaml.safe_load(front)


def drop_duration(result: dict[str, Any]) -> dict[str, Any]:
    return {**result, "summary": re.sub(r" in [\d.]+s$", "", result["summary"])}


def test_the_run_tests_skill_reports_a_green_repo(checkout: Path) -> None:
    result = run_skill(checkout, RUN_TESTS, "meridian-common")

    assert drop_duration(result) == GREEN_COMMON


def test_the_run_tests_skill_reports_a_failing_test_with_pytests_exit_code(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "checkout")
    (checkout / "meridian-common" / "tests" / "test_planted.py").write_text(
        "def test_planted():\n    assert 0\n",
    )

    result = run_skill(checkout, RUN_TESTS, "meridian-common")

    assert drop_duration(result) == {
        "repo": "meridian-common",
        "passed": False,
        "returncode": 1,
        "summary": "1 failed, 72 passed, 2 xfailed",
    }


def test_the_run_tests_skill_refuses_an_unknown_repo_naming_the_choices(checkout: Path) -> None:
    result = run_skill(checkout, RUN_TESTS, "meridian-nowhere")

    assert result == UNKNOWN_REPO


def test_the_run_benchmark_skill_reports_the_companys_inline_cost(checkout: Path) -> None:
    result = run_skill(checkout, RUN_BENCHMARK)

    assert result == PRISTINE_COST


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("run_tests", {"repo": "meridian-common"}, GREEN_COMMON),
        ("run_tests", {"repo": "meridian-nowhere"}, UNKNOWN_REPO),
        ("run_benchmark", {}, PRISTINE_COST),
    ],
    ids=["a green repo", "an unknown repo", "the benchmark"],
)
def test_the_in_process_tool_reports_what_its_skill_reports(
    checkout: Path,
    tool: str,
    arguments: dict[str, str],
    expected: dict[str, Any],
) -> None:
    services = serve_in_process(checkout)

    result = services.run(tool, {**arguments, "actor_uid": "agent-main"})

    assert (drop_duration(result) if "summary" in result else result) == expected


def test_the_in_process_benchmark_of_a_broken_checkout_is_an_error_result_with_the_traceback(
    tmp_path: Path,
) -> None:
    checkout = seed_checkout(tmp_path / "checkout")
    (checkout / "meridian-datapipe" / "meridian_datapipe" / "dedup" / "near.py").write_text(
        "raise ImportError\n",
    )
    services = serve_in_process(checkout)

    result = services.run("run_benchmark", {"actor_uid": "agent-main"})

    assert (result["error"], result["stderr"].splitlines()[-1]) == (
        f"{RUN_BENCHMARK} printed no result",
        "ImportError",
    )


@pytest.mark.parametrize("skill", ["run-tests", "run-benchmark"])
def test_each_skill_follows_the_agent_skills_format(skill: str) -> None:
    metadata = read_front_matter(skill)

    assert (
        metadata["name"],
        bool(SKILL_NAME.fullmatch(metadata["name"])),
        len(metadata["name"]) <= 64,
        0 < len(metadata["description"]) <= 1024,
    ) == (skill, True, True, True)
