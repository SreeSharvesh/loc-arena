"""Calibration metrics: expected calibration error and the Brier score.

:func:`expected_calibration_error` bins records by confidence and sums the gap between each bin's mean
confidence and its empirical accuracy, weighted by the bin's share of records. :func:`brier_score` is the
mean squared error between confidence and the correctness indicator. Both are deterministic pure functions
over a list of :class:`~meridian_evalkit.metrics.records.ScoreRecord`; lower is better for each.
"""

from __future__ import annotations

from collections.abc import Sequence

from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics.records import ScoreRecord


def _bin_index(confidence: float, n_bins: int) -> int:
    """The bin a confidence in ``[0, 1]`` falls into (the top edge lands in the last bin)."""
    if confidence >= 1.0:
        return n_bins - 1
    return int(confidence * n_bins)


def expected_calibration_error(records: Sequence[ScoreRecord], *, n_bins: int = 10) -> float:
    """The ECE of ``records`` over ``n_bins`` equal-width confidence bins (raises on empty input)."""
    if not records:
        raise MetricError("ECE needs at least one record", code="evalkit.metric")
    if n_bins < 1:
        raise MetricError("n_bins must be >= 1", code="evalkit.metric", n_bins=n_bins)
    conf_sum = [0.0] * n_bins
    hit_sum = [0.0] * n_bins
    count = [0] * n_bins
    for r in records:
        b = _bin_index(r.confidence, n_bins)
        conf_sum[b] += r.confidence
        hit_sum[b] += 1.0 if r.correct else 0.0
        count[b] += 1
    n = len(records)
    ece = 0.0
    for b in range(n_bins):
        if count[b] == 0:
            continue
        mean_conf = conf_sum[b] / count[b]
        mean_acc = hit_sum[b] / count[b]
        ece += (count[b] / n) * abs(mean_conf - mean_acc)
    return ece


def brier_score(records: Sequence[ScoreRecord]) -> float:
    """The mean squared error between confidence and the correctness indicator."""
    if not records:
        raise MetricError("Brier score needs at least one record", code="evalkit.metric")
    total = 0.0
    for r in records:
        target = 1.0 if r.correct else 0.0
        total += (r.confidence - target) ** 2
    return total / len(records)
