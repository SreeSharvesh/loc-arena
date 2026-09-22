"""Metrics: named metrics over scored records plus an incremental accumulator.

Owns the :class:`ScoreRecord` metrics consume, the classification metrics (accuracy, ``pass@k``), the
calibration metrics (ECE, Brier), the reference-embedding similarity metric, the :class:`RunningAccuracy`
accumulator, and the :class:`MetricRegistry` that names and evaluates them.
"""

from __future__ import annotations

from meridian_evalkit.metrics.accumulator import RunningAccuracy
from meridian_evalkit.metrics.calibration import brier_score, expected_calibration_error
from meridian_evalkit.metrics.classification import accuracy, error_rate, pass_at_k
from meridian_evalkit.metrics.embedding import (
    EMBED_DIM,
    embed,
    reference_embedding_similarity,
)
from meridian_evalkit.metrics.records import ScoreRecord
from meridian_evalkit.metrics.registry import Metric, MetricRegistry, default_registry
from meridian_evalkit.metrics.stratified import (
    group_by_category,
    macro_average,
    micro_average,
    stratified,
)

__all__ = [
    "EMBED_DIM",
    "Metric",
    "MetricRegistry",
    "RunningAccuracy",
    "ScoreRecord",
    "accuracy",
    "brier_score",
    "default_registry",
    "embed",
    "error_rate",
    "expected_calibration_error",
    "group_by_category",
    "macro_average",
    "micro_average",
    "pass_at_k",
    "reference_embedding_similarity",
    "stratified",
]
