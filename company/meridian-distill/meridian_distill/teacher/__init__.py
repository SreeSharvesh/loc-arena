"""The teacher client and the response/feature surface reused downstream."""

from __future__ import annotations

from meridian_distill.teacher.client import LogitFn, TeacherClient
from meridian_distill.teacher.response import (
    DEFAULT_FEATURE_DIM,
    TeacherResponse,
    compute_features,
    teacher_features,
)

__all__ = [
    "DEFAULT_FEATURE_DIM",
    "LogitFn",
    "TeacherClient",
    "TeacherResponse",
    "compute_features",
    "teacher_features",
]
