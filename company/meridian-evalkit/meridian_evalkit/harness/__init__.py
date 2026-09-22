"""Harness: run eval items through a model, score them, and annotate teacher features.

Owns the :class:`EvalItem`/:class:`Prediction` value types and the :class:`Model` protocol, a deterministic
reference model, the per-item teacher-feature computation reused from distill, and the :class:`Harness` that
reduces a run into a :class:`HarnessReport`.
"""

from __future__ import annotations

from meridian_evalkit.harness.features import item_features
from meridian_evalkit.harness.harness import Harness, HarnessReport, ItemResult
from meridian_evalkit.harness.model import DeterministicModel, EvalItem, Model, Prediction

__all__ = [
    "DeterministicModel",
    "EvalItem",
    "Harness",
    "HarnessReport",
    "ItemResult",
    "Model",
    "Prediction",
    "item_features",
]
