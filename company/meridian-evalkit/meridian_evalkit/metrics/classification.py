"""Classification metrics: accuracy and the unbiased ``pass@k`` estimator.

:func:`accuracy` is the fraction of records whose primary answer is correct. :func:`pass_at_k` is the standard
unbiased estimator of the probability that at least one of ``k`` samples is correct, averaged over items,
which is why it takes per-sample correctness rather than a single flag. Both are deterministic pure functions
over a list of :class:`~meridian_evalkit.metrics.records.ScoreRecord`.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import comb

from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics.records import ScoreRecord


def accuracy(records: Sequence[ScoreRecord]) -> float:
    """The fraction of ``records`` whose primary answer is correct (raises on an empty input)."""
    if not records:
        raise MetricError("accuracy needs at least one record", code="evalkit.metric")
    correct = sum(1 for r in records if r.correct)
    return correct / len(records)


def _pass_at_k_one(n: int, c: int, k: int) -> float:
    """The unbiased per-item ``pass@k``: probability a size-``k`` draw hits one of ``c`` correct of ``n``."""
    if k > n:
        raise MetricError("k must not exceed the sample count", code="evalkit.metric", k=k, n=n)
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_at_k(records: Sequence[ScoreRecord], k: int) -> float:
    """The unbiased ``pass@k`` averaged over ``records`` (each record supplies its per-sample flags)."""
    if not records:
        raise MetricError("pass@k needs at least one record", code="evalkit.metric")
    if k < 1:
        raise MetricError("k must be >= 1", code="evalkit.metric", k=k)
    total = 0.0
    for r in records:
        n = r.sample_count
        c = sum(1 for hit in r.samples if hit)
        total += _pass_at_k_one(n, c, k)
    return total / len(records)


def error_rate(records: Sequence[ScoreRecord]) -> float:
    """The complement of :func:`accuracy` (the fraction incorrect)."""
    return 1.0 - accuracy(records)
