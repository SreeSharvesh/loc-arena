"""The images ship only what their code imports, run as nonroot, and hand every fresh volume to nonroot.

The grader also runs scenario code the image does not ship: each scenario's ``measure.py``, bound read-only
into the grader at run time. It must import only what the sandbox image ships, which the probe below checks
by loading it exactly as the grader CLI does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from loc_arena.stack.constants import MIRROR_MOUNT_PATH, SEALED_MOUNT_PATH, WORKSPACE_MOUNT_PATH
from scenarios.loader import SCENARIOS_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
SANDBOX_MODULES = (
    "loc_arena.execution.app",
    "loc_arena.execution.client",
    "loc_arena.grader.__main__",
)
# Every path the sandbox stage may copy: the company seed, the code of the execution app, the grader and
# the edge, and their package __init__ files. Anything else, above all configs/, scenarios/ (the sealed
# reference) and live.py (covert briefs), stays out of the image agents' code runs in.
SANDBOX_IMAGE_ALLOWLIST = frozenset(
    {
        "company/",
        "loc_arena/__init__.py",
        "loc_arena/stack/",
        "loc_arena/execution/",
        "loc_arena/grader/",
        "loc_arena/gateway/__init__.py",
        "loc_arena/gateway/edge.py",
        "loc_arena/logging_/__init__.py",
        "loc_arena/logging_/events.py",
    },
)
MEASURE_MODULES = sorted(SCENARIOS_ROOT.glob("*/measure.py"))  # bound into the grader, one per scenario
# Imported in a fresh interpreter behind a finder that refuses every loc_arena module outside the sandbox's
# packages (config, live, scenarios, ...) and every scenario pack.
SANDBOX_ONLY_FINDER = """
import importlib.abc
import sys

ALLOWED = ("loc_arena.stack", "loc_arena.execution", "loc_arena.grader", "loc_arena.logging_.events")
PACKAGES = ("loc_arena", "loc_arena.logging_")


class SandboxOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        refused = name == "scenarios" or name.startswith("scenarios.") or (
            name.startswith("loc_arena")
            and name not in PACKAGES
            and not any(name == allowed or name.startswith(allowed + ".") for allowed in ALLOWED)
        )
        if refused:
            raise ImportError(f"sandbox code imported {name}")
        return None


sys.meta_path.insert(0, SandboxOnly())
"""
IMPORT_PROBE = SANDBOX_ONLY_FINDER + "for module in sys.argv[1:]:\n    __import__(module)\n"
# A scenario's measure.py, loaded by path as the grader CLI loads its mounted copy.
MEASURE_MODULE_PROBE = (
    SANDBOX_ONLY_FINDER
    + "from pathlib import Path\n"
    + "from loc_arena.grader.measure_steps import load_measure_module\n"
    + "load_measure_module(Path(sys.argv[1]))\n"
)


def _stages() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    current = ""
    for line in DOCKERFILE.read_text().splitlines():
        if line.startswith("FROM "):
            current = line.split(" AS ")[-1].strip()
            stages[current] = []
        elif current:
            stages[current].append(line)
    return stages


def _copied_sources(stage: list[str]) -> list[str]:
    return [
        source
        for line in stage
        if line.startswith("COPY ") and "--from" not in line
        for source in line.split()[1:-1]
    ]


def test_sandbox_code_imports_only_what_the_sandbox_image_ships() -> None:
    command = [sys.executable, "-c", IMPORT_PROBE, *SANDBOX_MODULES]

    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPOSITORY_ROOT, check=False)

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("measure_module", MEASURE_MODULES, ids=lambda path: path.parent.name)
def test_a_scenario_measure_module_imports_only_what_the_sandbox_image_ships(measure_module: Path) -> None:
    command = [sys.executable, "-c", MEASURE_MODULE_PROBE, str(measure_module)]

    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPOSITORY_ROOT, check=False)

    assert completed.returncode == 0, completed.stderr


def test_every_path_the_sandbox_stage_copies_exists() -> None:
    sources = _copied_sources(_stages()["sandbox"])
    assert sources  # a stage that copies nothing would pass vacuously

    missing = [source for source in sources if not (REPOSITORY_ROOT / source).exists()]

    assert missing == []


def test_the_sandbox_stage_copies_only_allowlisted_paths() -> None:
    sources = _copied_sources(_stages()["sandbox"])
    assert sources  # a stage that copies nothing would pass vacuously

    unlisted = [source for source in sources if source not in SANDBOX_IMAGE_ALLOWLIST]

    assert unlisted == []


def test_both_images_create_every_volume_mount_point_owned_by_nonroot() -> None:
    base = _stages()["base"]

    owned_by_nonroot = {
        argument
        for line in base
        if line.startswith("RUN install --directory --owner=nonroot --group=nonroot ")
        for argument in line.split()[5:]
    }

    assert {str(SEALED_MOUNT_PATH), str(MIRROR_MOUNT_PATH), str(WORKSPACE_MOUNT_PATH)} <= owned_by_nonroot


def test_the_sandbox_image_runs_as_nonroot() -> None:
    sandbox = _stages()["sandbox"]

    users = [line for line in sandbox if line.startswith("USER ")]

    assert users == ["USER nonroot"]


def test_the_app_stays_the_default_build_target() -> None:
    stages = list(_stages())

    assert stages[-1] == "app"
