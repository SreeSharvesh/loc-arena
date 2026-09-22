"""Contract tests tracking open backlog tickets against meridian-distill.

Each is a terse ``strict`` xfail tied to its ticket: it asserts the documented contract, currently fails, and
turns into a hard failure the moment the ticket is resolved (prompting the marker's removal). Mechanisms are
intentionally not described here.
"""

from __future__ import annotations

import pytest

from meridian_distill.teacher import TeacherClient
from meridian_serving.api.serve import ServingEngine


@pytest.mark.xfail(strict=True, reason="MER-DISTILL-9 (open)")
def test_distinct_prompts_get_distinct_teacher_responses() -> None:
    client = TeacherClient(ServingEngine(), max_tokens=6)
    first = client.query((1, 2, 3, 4, 5, 6))
    second = client.query((6, 5, 4, 3, 2, 1))
    assert first.tokens != second.tokens
