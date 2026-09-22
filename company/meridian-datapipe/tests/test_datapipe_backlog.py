"""Contract tests tracking open backlog tickets against meridian-datapipe.

Each is a terse ``strict`` xfail tied to its ticket: it asserts the documented contract, currently fails, and
turns into a hard failure the moment the ticket is resolved (prompting the marker's removal). Mechanisms are
intentionally not described here.
"""

from __future__ import annotations

import pytest

from meridian_datapipe.contamination import ContaminationFilter, HoldoutRegistry
from meridian_datapipe.dedup import exact_dedup
from meridian_datapipe.types import Document


@pytest.mark.xfail(strict=True, reason="MER-DATAPIPE-17 (open)")
def test_exact_dedup_never_merges_distinct_texts() -> None:
    a = Document("a", "x" * 64 + "AAAAA")
    b = Document("b", "x" * 64 + "BBBBB")
    result = exact_dedup([a, b])
    assert result.unique_count == 2


@pytest.mark.xfail(strict=True, reason="MER-DATAPIPE-23 (open)")
def test_sharded_holdout_still_flags_contamination() -> None:
    holdout = [
        Document("h0", "holdout sample document number 0"),
        Document("h1", "holdout sample document number 1"),
    ]
    registry = HoldoutRegistry.from_documents(holdout, num_shards=2)
    filt = ContaminationFilter(registry)
    assert filt.is_contaminated(Document("probe", "holdout sample document number 0"))
