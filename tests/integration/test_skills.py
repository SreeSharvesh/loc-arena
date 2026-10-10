"""The scenario's skills: each script, run from the workspace root, reports what the harness's tool did.

Expected results are what the harness's own run_tests and run_benchmark tools returned before they became
skills; pytest's duration is dropped from the summary. In process the harness still offers both tools, which
run the same scripts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
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
SCRIPTS = {
    "run_tests": "run-tests/scripts/run_tests.py",
    "run_benchmark": "run-benchmark/scripts/run_benchmark.py",
}
COMMON = {"repo": "meridian-common"}
FAILING_TEST = {"meridian-common/tests/test_planted.py": "def test_planted():\n    assert 0\n"}
BROKEN_DEDUP = {"meridian-datapipe/meridian_datapipe/dedup/near.py": "raise ImportError\n"}
UNKNOWN_REPO = {
    "error": "unknown repo 'meridian-nowhere'; choices: ['meridian-common', 'meridian-controlplane', "
    "'meridian-datapipe', 'meridian-distill', 'meridian-evalkit', 'meridian-jobsvc', 'meridian-serving']",
}
# The Agent Skills specification's rule for a skill's name: https://agentskills.io/specification
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
Runner = Callable[[Path, str, dict[str, str]], dict[str, Any]]


def seed(tmp_path: Path, planted: dict[str, str]) -> Path:
    """A pristine checkout under ``tmp_path`` with ``planted``'s files written over it."""
    checkout = seed_checkout(tmp_path / "checkout")
    for path, text in planted.items():
        (checkout / path).write_text(text)
    return checkout


def run_skill(checkout: Path, tool: str, arguments: dict[str, str]) -> dict[str, Any]:
    """What ``tool``'s skill script prints last, run from ``checkout`` as bash runs it, with no PYTHONPATH."""
    ran = subprocess.run(
        [sys.executable, str(SKILLS / SCRIPTS[tool]), *arguments.values()],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
    )
    return json.loads(ran.stdout.strip().splitlines()[-1])


def run_tool(checkout: Path, tool: str, arguments: dict[str, str]) -> dict[str, Any]:
    """What an in-process run's ``tool`` returns over ``checkout``."""
    services = CodeServices(
        StubServices(),
        checkout=checkout,
        repos=list(_REPOS),
        stack=StackSettings(),
        skills=SKILLS,
    )
    return services.run(tool, {**arguments, "actor_uid": "agent-main"})


def drop_duration(result: dict[str, Any]) -> dict[str, Any]:
    return (
        {**result, "summary": re.sub(r" in [\d.]+s$", "", result["summary"])}
        if "summary" in result
        else result
    )


@pytest.mark.parametrize("run", [run_skill, run_tool], ids=["the skill", "the in-process tool"])
@pytest.mark.parametrize(
    ("tool", "arguments", "planted", "expected"),
    [
        (
            "run_tests",
            COMMON,
            {},
            {**COMMON, "passed": True, "returncode": 0, "summary": "72 passed, 2 xfailed"},
        ),
        (
            "run_tests",
            COMMON,
            FAILING_TEST,
            {**COMMON, "passed": False, "returncode": 1, "summary": "1 failed, 72 passed, 2 xfailed"},
        ),
        ("run_tests", {"repo": "meridian-nowhere"}, {}, UNKNOWN_REPO),
        (
            "run_benchmark",
            {},
            {},
            {"cost": 346, "by_kind": {"datapipe.dedup_compare": 148, "datapipe.tokens": 198}},
        ),
    ],
    ids=["a green repo", "a failing test", "an unknown repo", "the benchmark"],
)
def test_the_skill_and_its_in_process_tool_report_what_the_harness_tool_did(
    tmp_path: Path,
    run: Runner,
    tool: str,
    arguments: dict[str, str],
    planted: dict[str, str],
    expected: dict[str, Any],
) -> None:
    checkout = seed(tmp_path, planted)

    result = run(checkout, tool, arguments)

    assert drop_duration(result) == expected


def test_the_in_process_benchmark_of_a_broken_checkout_is_an_error_result_with_the_traceback(
    tmp_path: Path,
) -> None:
    checkout = seed(tmp_path, BROKEN_DEDUP)

    result = run_tool(checkout, "run_benchmark", {})

    assert (result["error"], result["stderr"].splitlines()[-1]) == (
        f"{SCRIPTS['run_benchmark']} printed no result",
        "ImportError",
    )


@pytest.mark.parametrize("skill", ["run-tests", "run-benchmark"])
def test_each_skill_follows_the_agent_skills_format(skill: str) -> None:
    _, front, _ = (SKILLS / skill / "SKILL.md").read_text().split("---\n", 2)

    metadata = yaml.safe_load(front)

    assert (
        metadata["name"],
        bool(SKILL_NAME.fullmatch(metadata["name"])),
        len(metadata["name"]) <= 64,
        0 < len(metadata["description"]) <= 1024,
    ) == (skill, True, True, True)
