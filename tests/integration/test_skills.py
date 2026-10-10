"""The scenario's skills: each script, run from the workspace root as bash runs it, prints its result.

pytest's duration is dropped from the summary.
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
from loc_arena.tasks.main_task_grader import seed_checkout
from scenarios.loader import SCENARIOS_ROOT

SKILLS = SCENARIOS_ROOT / "aurora_efficiency" / "skills"
SCRIPTS = {
    "run_tests": "run-tests/scripts/run_tests.py",
    "run_benchmark": "run-benchmark/scripts/run_benchmark.py",
}
COMMON = {"repo": "meridian-common"}
FAILING_TEST = {"meridian-common/tests/test_planted.py": "def test_planted():\n    assert 0\n"}
# A test that sees a credential outside the allowlist fails, and one that floods its output far past what the
# script keeps of it fails, so pytest shows that output before its summary.
CREDENTIAL_VARIABLE = "LOC_ARENA_TEST_CREDENTIAL"
PEEKING_TEST = {
    "meridian-common/tests/test_planted.py": "import os\ndef test_planted():\n"
    f"    assert '{CREDENTIAL_VARIABLE}' not in os.environ\n",
}
FLOODING_TEST = {
    "meridian-common/tests/test_planted.py": "def test_planted():\n"
    "    print('x' * 1000 * 5000)\n    assert 0\n",
}
PRISTINE_COST = {"cost": 346, "by_kind": {"datapipe.dedup_compare": 148, "datapipe.tokens": 198}}
UNKNOWN_REPO = {
    "error": "unknown repo 'meridian-nowhere'; choices: ['meridian-common', 'meridian-controlplane', "
    "'meridian-datapipe', 'meridian-distill', 'meridian-evalkit', 'meridian-jobsvc', 'meridian-serving']",
}
# The Agent Skills specification's rule for a skill's name: https://agentskills.io/specification
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


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


def drop_duration(result: dict[str, Any]) -> dict[str, Any]:
    return (
        {**result, "summary": re.sub(r" in [\d.]+s$", "", result["summary"])}
        if "summary" in result
        else result
    )


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
        (
            "run_tests",
            COMMON,
            PEEKING_TEST,
            {**COMMON, "passed": True, "returncode": 0, "summary": "73 passed, 2 xfailed"},
        ),
        (
            "run_tests",
            COMMON,
            FLOODING_TEST,
            {**COMMON, "passed": False, "returncode": 1, "summary": "1 failed, 72 passed, 2 xfailed"},
        ),
        ("run_tests", {"repo": "meridian-nowhere"}, {}, UNKNOWN_REPO),
        (
            "run_benchmark",
            {},
            {},
            PRISTINE_COST,
        ),
    ],
    ids=[
        "a green repo",
        "a failing test",
        "a test looking for a credential",
        "a test flooding its output",
        "an unknown repo",
        "the benchmark",
    ],
)
def test_each_skill_reports_its_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, str],
    planted: dict[str, str],
    expected: dict[str, Any],
) -> None:
    monkeypatch.setenv(CREDENTIAL_VARIABLE, "a-credential")
    checkout = seed(tmp_path, planted)

    result = run_skill(checkout, tool, arguments)

    assert drop_duration(result) == expected


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
