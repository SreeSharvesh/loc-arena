from __future__ import annotations

import pytest

from meridian_distill.errors import SelectionError
from meridian_distill.select import Candidate, Selector, select_by_coverage, select_by_uncertainty


def _cands() -> list[Candidate]:
    return [
        Candidate("peaked", (1.0, 0.0, 0.0, 0.0)),
        Candidate("uniform", (0.25, 0.25, 0.25, 0.25)),
        Candidate("mid", (0.5, 0.5, 0.0, 0.0)),
    ]


def test_uncertainty_prefers_the_flat_distribution() -> None:
    picked = select_by_uncertainty(_cands(), 1)
    assert [c.item_id for c in picked] == ["uniform"]


def test_uncertainty_is_deterministic() -> None:
    a = select_by_uncertainty(_cands(), 2)
    b = select_by_uncertainty(_cands(), 2)
    assert [c.item_id for c in a] == [c.item_id for c in b]


def test_coverage_spreads_and_is_deterministic() -> None:
    cands = [
        Candidate("a", (0.0, 0.0)),
        Candidate("b", (0.1, 0.0)),
        Candidate("c", (1.0, 1.0)),
    ]
    picked = select_by_coverage(cands, 2)
    ids = {c.item_id for c in picked}
    assert ids == {"a", "c"}
    assert [c.item_id for c in select_by_coverage(cands, 2)] == [c.item_id for c in picked]


def test_k_at_or_above_pool_returns_all_sorted() -> None:
    picked = Selector("coverage").select(_cands(), 10)
    assert [c.item_id for c in picked] == ["mid", "peaked", "uniform"]


def test_zero_k_selects_nothing() -> None:
    assert Selector().select(_cands(), 0) == []


def test_unknown_criterion_is_rejected() -> None:
    with pytest.raises(SelectionError):
        Selector("random")


def test_negative_k_is_rejected() -> None:
    with pytest.raises(SelectionError):
        Selector().select(_cands(), -1)
