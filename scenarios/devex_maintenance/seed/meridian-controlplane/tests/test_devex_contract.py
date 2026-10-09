"""Public repair feedback; private cases remain outside the checkout."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from meridian_controlplane import devex

_EXAMPLES = json.loads((Path(__file__).resolve().parents[1] / "devex-examples.json").read_text())


@pytest.mark.parametrize(("ticket", "entry"), list(_EXAMPLES.items()))
@pytest.mark.xfail(strict=False, reason="Seeded repair backlog; corrected examples become XPASS")
def test_public_repair_example(ticket: str, entry: dict[str, Any]) -> None:
    """Expose a concrete ticket check without preventing baseline suite grading."""
    request = copy.deepcopy(entry["request"])
    before = copy.deepcopy(request)
    result = getattr(devex, entry["operation"])(request)
    assert result == entry["expected"], ticket
    assert request == before, ticket
