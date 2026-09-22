from __future__ import annotations

import pytest

from meridian_jobsvc.dsl import parse_spec
from meridian_jobsvc.errors import SpecSyntaxError


def test_parse_minimal_spec() -> None:
    spec = parse_spec("job eval")
    assert spec.name == "eval"
    assert spec.command == ["run", "eval"]
    assert spec.cpu == 1.0
    assert spec.memory_mb == 512
    assert spec.priority == 0
    assert spec.depends_on == ()


def test_parse_full_spec() -> None:
    spec = parse_spec("job eval cpu=2 mem=1024 prio=high needs=prep,tok cmd=python:eval.py")
    assert spec.cpu == 2.0
    assert spec.memory_mb == 1024
    assert spec.priority == 20
    assert spec.depends_on == ("prep", "tok")
    assert spec.command == ["python", "eval.py"]


def test_priority_words_and_integers() -> None:
    assert parse_spec("job x prio=low").priority == 0
    assert parse_spec("job x prio=normal").priority == 10
    assert parse_spec("job x prio=7").priority == 7


@pytest.mark.parametrize(
    "text",
    [
        "",
        "eval cpu=1",  # missing 'job' head
        "job",  # missing name
        "job cpu=1",  # name looks like a field
        "job x cpu=abc",
        "job x mem=1.5",
        "job x prio=huge",
        "job x cpu=-1",
        "job x bogus=1",  # unknown field
        "job x cpu",  # not key=value
        "job x cpu=",  # empty value
        "job x needs=",  # empty dependency list
    ],
)
def test_malformed_specs_raise(text: str) -> None:
    with pytest.raises(SpecSyntaxError):
        parse_spec(text)


def test_duplicate_field_rejected() -> None:
    with pytest.raises(SpecSyntaxError):
        parse_spec("job x cpu=1 cpu=2")


def test_parsing_is_deterministic() -> None:
    text = "job eval cpu=2 mem=1024 prio=high needs=a,b"
    assert parse_spec(text) == parse_spec(text)
