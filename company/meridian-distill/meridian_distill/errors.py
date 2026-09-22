"""Distill-specific errors, extending the shared Meridian hierarchy.

The distillation platform raises these so callers can tell a prompt fault from a teacher (inference) fault
from a curriculum, selection, or writer fault. They all derive from
:class:`meridian_common.errors.MeridianError`, so a generic handler still catches them, while the stable
``code`` keys alerts on the specific stage.
"""

from __future__ import annotations

from meridian_common.errors import MeridianError, ValidationError


class DistillError(MeridianError):
    """Base class for every distillation error."""

    code = "distill.error"


class PromptError(DistillError, ValidationError):
    """A prompt could not be rendered from a document and its few-shot context."""

    code = "distill.prompt"


class TeacherError(DistillError):
    """The teacher client could not run inference or produce a response through the serving stack."""

    code = "distill.teacher"


class CurriculumError(DistillError):
    """A curriculum could not be assembled (empty corpus or an unknown ordering)."""

    code = "distill.curriculum"


class SelectionError(DistillError):
    """A data-selection pass was given an invalid budget or criterion."""

    code = "distill.select"


class WriterError(DistillError):
    """The dataset writer was given inconsistent records or a duplicate record id."""

    code = "distill.writer"
