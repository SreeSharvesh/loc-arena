"""Per-item teacher features for the harness, computed through the distill feature surface.

The harness annotates every scored item with a fixed-width teacher-feature vector. The vector is produced by
the distillation platform's public feature surface (:func:`meridian_distill.teacher.compute_features` wrapped
in a :class:`~meridian_distill.teacher.TeacherResponse` and read back through
:func:`meridian_distill.teacher.teacher_features`), so the harness and distill agree on feature shape and
values by construction. The features are derived from a prediction's tokens each time they are requested.
"""

from __future__ import annotations

from meridian_common import cost
from meridian_distill.teacher import (
    DEFAULT_FEATURE_DIM,
    TeacherResponse,
    compute_features,
    teacher_features,
)
from meridian_evalkit.harness.model import EvalItem, Prediction


def item_features(
    item: EvalItem,
    prediction: Prediction,
    *,
    dim: int = DEFAULT_FEATURE_DIM,
) -> tuple[float, ...]:
    """The teacher-feature vector for ``item``'s ``prediction``, via the distill feature surface.

    A :class:`~meridian_distill.teacher.TeacherResponse` is assembled from the prediction's tokens using
    distill's :func:`~meridian_distill.teacher.compute_features`, and the vector is read back through
    :func:`~meridian_distill.teacher.teacher_features`.
    """
    cost.record("evalkit.feature_recompute", 1)  # a per-item teacher-feature recompute
    response = TeacherResponse(
        request_key=f"eval-{item.item_id}",
        prompt=item.prompt,
        tokens=prediction.tokens,
        features=compute_features(prediction.tokens, dim=dim),
    )
    return teacher_features(response)
