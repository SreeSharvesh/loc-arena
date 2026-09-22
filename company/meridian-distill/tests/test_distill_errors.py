from __future__ import annotations

from meridian_common.errors import MeridianError, ValidationError
from meridian_distill.errors import (
    CurriculumError,
    DistillError,
    PromptError,
    SelectionError,
    TeacherError,
    WriterError,
)


def test_all_distill_errors_are_meridian_errors() -> None:
    for exc in (PromptError, TeacherError, CurriculumError, SelectionError, WriterError):
        assert issubclass(exc, DistillError)
        assert issubclass(exc, MeridianError)


def test_prompt_error_is_a_validation_error() -> None:
    assert issubclass(PromptError, ValidationError)


def test_error_codes_are_stable() -> None:
    assert DistillError.code == "distill.error"
    assert PromptError.code == "distill.prompt"
    assert TeacherError.code == "distill.teacher"
    assert CurriculumError.code == "distill.curriculum"
    assert SelectionError.code == "distill.select"
    assert WriterError.code == "distill.writer"


def test_error_carries_context() -> None:
    err = TeacherError("boom", max_tokens=0)
    assert err.context["max_tokens"] == 0
    assert err.to_dict()["code"] == "distill.teacher"
