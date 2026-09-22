"""CI pipeline definitions: stages, a required-stage gate, and stage results.

A :class:`Pipeline` is an ordered set of named :class:`Stage` s (for example ``lint``, ``typecheck``,
``test``), each of which may be required or advisory. :class:`StageResults` records the pass/fail outcome of a
run, and :meth:`StageResults.all_required_green` is the green-CI gate a merge decision is built on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from meridian_controlplane.errors import CiConfigError


@dataclass(frozen=True)
class Stage:
    """One CI stage: a name and whether it must pass for the pipeline to be considered green."""

    name: str
    required: bool = True


@dataclass(frozen=True)
class Pipeline:
    """An ordered set of CI stages for a repo."""

    stages: tuple[Stage, ...]

    def __post_init__(self) -> None:
        """Reject an empty pipeline or duplicate stage names."""
        if not self.stages:
            raise CiConfigError("pipeline must declare at least one stage", path="stages")
        names = [stage.name for stage in self.stages]
        if len(set(names)) != len(names):
            raise CiConfigError("pipeline stage names must be unique", path="stages")

    @classmethod
    def of(cls, *names: str) -> Pipeline:
        """A pipeline of required stages named ``names`` in order."""
        return cls(stages=tuple(Stage(name) for name in names))

    def stage_names(self) -> tuple[str, ...]:
        """The stage names in declaration order."""
        return tuple(stage.name for stage in self.stages)

    def required_names(self) -> tuple[str, ...]:
        """The names of the required stages in declaration order."""
        return tuple(stage.name for stage in self.stages if stage.required)


@dataclass(frozen=True)
class StageResults:
    """The pass/fail outcome of each stage in a pipeline run."""

    outcomes: Mapping[str, bool]

    @classmethod
    def of(cls, **outcomes: bool) -> StageResults:
        """Build results from keyword ``stage=passed`` pairs."""
        return cls(outcomes=dict(outcomes))

    def passed(self, stage: str) -> bool:
        """Whether ``stage`` passed (a stage with no recorded outcome counts as failed)."""
        return self.outcomes.get(stage, False)

    def all_required_green(self, pipeline: Pipeline) -> bool:
        """Whether every required stage of ``pipeline`` passed in this run."""
        return all(self.passed(name) for name in pipeline.required_names())

    def failed_required(self, pipeline: Pipeline) -> tuple[str, ...]:
        """The required stages that did not pass, in declaration order."""
        return tuple(name for name in pipeline.required_names() if not self.passed(name))


def merge_reasons(*reasons: Iterable[str]) -> tuple[str, ...]:
    """Flatten and de-duplicate reason fragments while preserving first-seen order."""
    seen: dict[str, None] = {}
    for group in reasons:
        for reason in group:
            seen.setdefault(reason, None)
    return tuple(seen)
