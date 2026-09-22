"""Tests for per-category (stratified) metric breakdowns."""

from __future__ import annotations

import pytest

from meridian_evalkit.errors import MetricError
from meridian_evalkit.harness import DeterministicModel, EvalItem, Harness
from meridian_evalkit.metrics import (
    ScoreRecord,
    group_by_category,
    macro_average,
    micro_average,
    stratified,
)


def _records() -> list[ScoreRecord]:
    return [
        ScoreRecord(item_id="a", correct=True, category="math"),
        ScoreRecord(item_id="b", correct=False, category="math"),
        ScoreRecord(item_id="c", correct=True, category="code"),
        ScoreRecord(item_id="d", correct=True, category="code"),
    ]


def test_group_by_category() -> None:
    groups = group_by_category(_records())
    assert set(groups) == {"math", "code"}
    assert len(groups["math"]) == 2
    assert len(groups["code"]) == 2


def test_stratified_accuracy() -> None:
    per = stratified(_records())
    assert per["math"] == pytest.approx(0.5)
    assert per["code"] == pytest.approx(1.0)


def test_macro_average_weights_categories_equally() -> None:
    # math=0.5, code=1.0 -> macro is their unweighted mean 0.75
    assert macro_average(_records()) == pytest.approx(0.75)


def test_micro_average_weights_records_equally() -> None:
    # 3 of 4 records correct -> micro is 0.75 here (categories are equal-sized)
    assert micro_average(_records()) == pytest.approx(0.75)


def test_macro_and_micro_differ_on_unbalanced_categories() -> None:
    records = [
        ScoreRecord(item_id="a", correct=True, category="big"),
        ScoreRecord(item_id="b", correct=True, category="big"),
        ScoreRecord(item_id="c", correct=True, category="big"),
        ScoreRecord(item_id="d", correct=False, category="small"),
    ]
    # macro: (1.0 + 0.0) / 2 = 0.5 ; micro: 3/4 = 0.75
    assert macro_average(records) == pytest.approx(0.5)
    assert micro_average(records) == pytest.approx(0.75)


def test_stratified_empty_raises() -> None:
    with pytest.raises(MetricError):
        stratified([])


def test_harness_report_accuracy_by_category() -> None:
    items = [EvalItem(item_id=f"m{i}", prompt=(1, i), reference=(i,), category="math") for i in range(5)] + [
        EvalItem(item_id=f"c{i}", prompt=(2, i), reference=(i,), category="code") for i in range(5)
    ]
    report = Harness().run(DeterministicModel(accuracy=1.0), items)
    by_cat = report.accuracy_by_category()
    assert by_cat == {"math": 1.0, "code": 1.0}
