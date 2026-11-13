"""Unit tests run as on a machine that names no certificate bundle of its own, wherever they run."""

from __future__ import annotations

import pytest
from loc_arena.machine_roots import ROOTS_VARIABLE


@pytest.fixture(autouse=True)
def no_machine_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROOTS_VARIABLE, raising=False)
