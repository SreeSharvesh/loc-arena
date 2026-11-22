"""The stack slots plugin: modules marked ``stack`` run ``stack_slots`` at once at most, a shard its share."""

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
RECORDS_ITS_RUN = """
from pathlib import Path


def test_records_its_run() -> None:
    Path(__file__).with_suffix(".ran").touch()
"""
MARKED = "import pytest\n\npytestmark = pytest.mark.stack{seconds}\n" + RECORDS_ITS_RUN
# One long module and four short ones: the last gives no seconds, and counts for 30.
SHARED_OUT = {
    "test_stack_0": "(seconds=90)",
    "test_stack_1": "(seconds=30)",
    "test_stack_2": "(seconds=30)",
    "test_stack_3": "(seconds=30)",
    "test_stack_4": "",
}


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


@pytest.mark.parametrize(
    ("shard", "ran"),
    [
        # The long module, then three short ones to the other share; the last evens the first share up.
        ("1/2", ["test_other", "test_stack_0", "test_stack_4"]),
        ("2/2", ["test_other", "test_stack_1", "test_stack_2", "test_stack_3"]),
    ],
)
def test_a_shard_runs_its_even_share_of_the_marked_modules_and_every_unmarked_one(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    shard: str,
    ran: list[str],
) -> None:
    pytester.makeini("[pytest]\naddopts = -p tests.stack_slots\nstack_slots = 1\nstack_slot_seconds = 60\n")
    marked = {name: MARKED.format(seconds=seconds) for name, seconds in SHARED_OUT.items()}
    pytester.makepyfile(test_other=RECORDS_ITS_RUN, **marked)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))

    result = pytester.runpytest_subprocess("--stack-shard", shard)

    recorded = sorted(path.stem for path in pytester.path.glob("*.ran"))
    assert (result.ret, recorded) == (pytest.ExitCode.OK, ran)
