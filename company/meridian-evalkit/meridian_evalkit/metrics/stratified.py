"""Per-category (stratified) metric breakdowns.

Benchmarks report not just an overall number but how a model does on each slice of the data. These helpers
group :class:`~meridian_evalkit.metrics.records.ScoreRecord` by category and apply a metric within each group.
:func:`stratified` returns the per-category value of any metric; :func:`macro_average` is the unweighted mean
across categories (every category counts equally, regardless of size), which contrasts with a micro-average
that weights by category size. All are deterministic pure functions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics.classification import accuracy
from meridian_evalkit.metrics.records import ScoreRecord

MetricFn = Callable[[Sequence[ScoreRecord]], float]


def group_by_category(records: Sequence[ScoreRecord]) -> dict[str, list[ScoreRecord]]:
    """Partition ``records`` into a mapping from category to the records in that category."""
    groups: dict[str, list[ScoreRecord]] = {}
    for record in records:
        groups.setdefault(record.category, []).append(record)
    return groups


def stratified(records: Sequence[ScoreRecord], metric: MetricFn = accuracy) -> dict[str, float]:
    """The value of ``metric`` within each category, keyed by category (raises on an empty input)."""
    if not records:
        raise MetricError("stratified metric needs at least one record", code="evalkit.metric")
    return {category: metric(group) for category, group in group_by_category(records).items()}


def macro_average(records: Sequence[ScoreRecord], metric: MetricFn = accuracy) -> float:
    """The unweighted mean of ``metric`` across categories (every category weighted equally)."""
    per_category = stratified(records, metric)
    return sum(per_category.values()) / len(per_category)


def micro_average(records: Sequence[ScoreRecord], metric: MetricFn = accuracy) -> float:
    """The value of ``metric`` over all records at once (each record weighted equally)."""
    if not records:
        raise MetricError("micro metric needs at least one record", code="evalkit.metric")
    return metric(records)
