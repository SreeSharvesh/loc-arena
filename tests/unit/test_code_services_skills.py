"""The read-only file tools reach the skills folder the brief points agents at; writes stay in the checkout.

A stack run's brief tells agents to read each skill's SKILL.md in the folder their sandbox mounts, so
``read_file`` and ``list_dir`` read that folder too, and nothing outside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.settings import StackSettings

MOUNTED = Path("/skills")  # where the brief says the skills are
SKILL = "# Run a repo's tests\n"


@pytest.fixture
def services(tmp_path: Path) -> CodeServices:
    (tmp_path / "skills" / "run-tests").mkdir(parents=True)
    (tmp_path / "skills" / "run-tests" / "SKILL.md").write_text(SKILL)
    (tmp_path / "checkout").mkdir()
    return CodeServices(
        checkout=tmp_path / "checkout",
        repos=[],
        stack=StackSettings(),
        skills=(MOUNTED, tmp_path / "skills"),
    )


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "read_file",
            {"path": "/skills/run-tests/SKILL.md"},
            {"path": "/skills/run-tests/SKILL.md", "content": SKILL, "truncated": False},
        ),
        (
            "list_dir",
            {"path": "/skills"},
            {"path": "/skills", "entries": [{"name": "run-tests", "type": "dir"}]},
        ),
        (
            "read_file",
            {"path": "/skills/../secret"},
            {
                "error": "bad args for read_file: path escapes the checkout: /skills/../secret",
                "tool": "read_file",
            },
        ),
        (
            "write_file",
            {"path": "/skills/run-tests/SKILL.md", "content": "overwritten"},
            {
                "error": "bad args for write_file: path escapes the checkout: /skills/run-tests/SKILL.md",
                "tool": "write_file",
            },
        ),
    ],
    ids=["a skill's SKILL.md", "the skills folder", "a path that leaves the skills folder", "a write"],
)
def test_the_file_tools_reach_the_skills_folder_for_reading_only(
    services: CodeServices,
    tool: str,
    arguments: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    result = services.run(tool, arguments)

    assert result == expected


LONG_NAME = "x" * 300  # longer than any filesystem's name limit


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("read_file", f"/skills/{LONG_NAME}"),
        ("read_file", LONG_NAME),
        ("list_dir", LONG_NAME),
        ("write_file", LONG_NAME),
    ],
    ids=["a skill", "a file", "a folder", "a write"],
)
def test_a_path_the_filesystem_refuses_is_an_error_result(
    services: CodeServices,
    tool: str,
    path: str,
) -> None:
    result = services.run(tool, {"path": path, "content": ""})

    assert result == {"error": f"{tool} failed: File name too long", "tool": tool}
