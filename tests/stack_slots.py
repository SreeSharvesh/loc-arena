"""A pytest plugin: at most ``stack_slots`` test modules marked ``stack`` run at once, across xdist workers.

Each such module starts its own Docker containers, most a whole stack. Several stacks starting together on a
machine of a few cores starve each other of CPU, so a container misses its health budget and its stack does
not come up. A marked module holds one slot, a lock file every worker of the session shares, while it runs.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from filelock import FileLock, Timeout

SLOT_POLL_SECONDS = 1.0  # how often a module waiting for a slot tries the slots again


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "stack_slots",
        "Test modules marked stack that may run at once, across workers.",
        type="int",
    )
    parser.addini(
        "stack_slot_seconds",
        "The longest a marked module waits for a slot; then it fails, naming the wait.",
        type="int",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "stack: the module starts Docker containers, in one of stack_slots")
    for name in ("stack_slots", "stack_slot_seconds"):
        if config.getini(name) < 1:  # an unset int ini option reads 0: no slot, or no wait
            raise pytest.UsageError(f"{name} must be at least 1")


@pytest.fixture(scope="module", autouse=True)
def stack_slot(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Hold one of the session's slots while a module marked ``stack`` runs; any other module holds none."""
    if request.node.get_closest_marker("stack") is None:
        yield
        return
    own = tmp_path_factory.getbasetemp()
    # An xdist worker's directory sits in its session's, shared by every worker; else it is the session's.
    shared = own.parent if hasattr(request.config, "workerinput") else own
    slots = [
        FileLock(shared / f"stack-slot-{number}.lock")
        for number in range(request.config.getini("stack_slots"))
    ]
    deadline = time.monotonic() + request.config.getini("stack_slot_seconds")
    while (held := next((slot for slot in slots if _take(slot)), None)) is None:
        if time.monotonic() > deadline:
            pytest.fail(f"no stack slot was free in {request.config.getini('stack_slot_seconds')} s")
        time.sleep(SLOT_POLL_SECONDS)
    try:
        yield
    finally:
        held.release()


def _take(slot: FileLock) -> bool:
    """Whether ``slot`` was free, and is now held."""
    try:
        slot.acquire(blocking=False)
    except Timeout:
        return False
    return True
