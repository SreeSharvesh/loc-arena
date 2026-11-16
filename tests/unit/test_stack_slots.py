"""The stack slots plugin: modules marked ``stack`` run at most ``stack_slots`` at once, across workers."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[2]
MODULES = 4
# Each marked module records when it held its slot. It holds it for 3 s, so workers that start apart overlap.
MODULE = """
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.stack


def test_holds_its_slot() -> None:
    started = time.monotonic()
    time.sleep(3)
    Path(__file__).with_suffix(".held").write_text(f"{started} {time.monotonic()}")
"""


@pytest.mark.parametrize("slots", [1, 2])
def test_no_more_marked_modules_run_at_once_than_the_session_has_slots(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    slots: int,
) -> None:
    pytester.makeini(
        f"[pytest]\naddopts = -p tests.stack_slots\nstack_slots = {slots}\nstack_slot_seconds = 60\n",
    )
    pytester.makepyfile(**{f"test_stack_{number}": MODULE for number in range(MODULES)})
    monkeypatch.setenv("PYTHONPATH", str(ROOT))  # where the subprocess imports the plugin from

    result = pytester.runpytest_subprocess("-n", str(MODULES), "--dist", "loadscope", "-p", "xdist")

    held = [tuple(map(float, path.read_text().split())) for path in sorted(pytester.path.glob("*.held"))]
    most = max((sum(start <= moment < end for start, end in held) for moment, _ in held), default=0)
    assert (result.ret, len(held), most) == (pytest.ExitCode.OK, MODULES, slots)
