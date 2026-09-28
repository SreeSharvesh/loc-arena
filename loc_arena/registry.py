"""Scorer and verifier registries (dispatch by name).

Enforces config over code: ``config.main_task.scorer`` and
``config.side_task.verifier`` are dispatched BY NAME through these registries, not hardcoded. Adding a
(main task, side task) pair at a new point becomes: register a scorer and a verifier by name and reference
them from config. A verifier's landing-predicate STRUCTURE stays the identity of that verifier version;
only the numeric thresholds are config.

A main-task scorer is two steps under one name (:class:`ScorerSteps`): the measure step, which runs where the
checkout is (the networkless grader container in the stack, this machine for STACK=0) and prints a typed
measurement, and the score step, which runs on the host against the scenario's sealed reference. Both modes
score the measurement's JSON line through :meth:`ScorerSteps.grade_output`, so they grade through one path.

This module holds only the registries and their types (no heavy imports), so config, scenarios, and
verifiers can import it without cycles: ``RunConfig`` and ``MainTaskScore`` are imported for annotations only.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from loc_arena.grader.measure_steps import MeasurementRequest, MeasureStep, get_measure_step
from loc_arena.stack.contracts import ContractModel

if TYPE_CHECKING:  # annotations only: config and main_task_grader import this module
    from loc_arena.config import RunConfig
    from loc_arena.tasks.main_task_grader import MainTaskScore

_Verifier = Callable[..., Any]

VERIFIER_REGISTRY: dict[str, _Verifier] = {}

V = TypeVar("V", bound=_Verifier)


class RegistryError(KeyError):
    """A scorer/verifier name was requested that is not registered."""


class MainTaskScorer(Protocol):
    """A registered main-task scorer as the harness drives it, whatever its measurement type."""

    @property
    def name(self) -> str:
        """The name ``config.main_task.scorer`` dispatches to."""
        ...

    def grade_checkout(
        self,
        checkout: Path,
        repositories: tuple[str, ...],
        config: RunConfig,
        reference_directory: Path,
    ) -> MainTaskScore:
        """Measure ``checkout`` on this machine (STACK=0), then score it as the grader's output is scored."""
        ...

    def grade_output(self, output: bytes, config: RunConfig, reference_directory: Path) -> MainTaskScore:
        """Score what a grader printed, read as untrusted."""
        ...


@dataclass(frozen=True)
class ScorerSteps[M: ContractModel]:
    """One scenario's main-task scorer: its measure step and its score step, over one measurement type.

    ``score`` takes the measurement, the run config and the scenario's sealed ``reference/`` directory, which
    it reads on the host only.
    """

    measure_step: MeasureStep[M]
    score: Callable[[M, RunConfig, Path], MainTaskScore]

    @property
    def name(self) -> str:
        """The scorer's name: its measure step's."""
        return self.measure_step.name

    def grade_checkout(
        self,
        checkout: Path,
        repositories: tuple[str, ...],
        config: RunConfig,
        reference_directory: Path,
    ) -> MainTaskScore:
        """STACK=0: run the measure step here (the harness files read from the reference directory).

        Its measurement goes through the same JSON line and parse as the grader container's output.
        """
        request = MeasurementRequest(
            checkout=checkout,
            harness_directory=reference_directory,
            repositories=repositories,
            settings=config.settings.grading,
        )
        output = self.measure_step.measure_as_json(request).encode()
        return self.grade_output(output, config, reference_directory)

    def grade_output(self, output: bytes, config: RunConfig, reference_directory: Path) -> MainTaskScore:
        """Score the measurement a grader printed; an output that is none scores the step's failed one."""
        try:
            measurement = self.measure_step.parse(output, config.settings.grading)
        except ValueError as error:  # pydantic's ValidationError included
            print(
                f"the grader printed no valid {self.name} measurement ({error}); graded as its failed one",
                file=sys.stderr,
            )
            measurement = self.measure_step.failed_measurement
        return self.score(measurement, config, reference_directory)


SCORER_REGISTRY: dict[str, MainTaskScorer] = {}


def register_scorer[M: ContractModel](scorer: ScorerSteps[M]) -> None:
    """Register a main-task scorer under its name.

    Its measure step must be the one registered under the same name in the grader's registry (the grader
    container finds it there), and a different scorer under the same name is an error.
    """
    if get_measure_step(scorer.name) is not scorer.measure_step:
        raise ValueError(f"scorer {scorer.name!r} pairs a measure step other than the one registered for it")
    registered = SCORER_REGISTRY.get(scorer.name)
    if registered is not None and registered is not scorer:
        raise ValueError(f"scorer {scorer.name!r} is already registered to a different scorer")
    SCORER_REGISTRY[scorer.name] = scorer


def register_verifier(name: str) -> Callable[[V], V]:
    """Register a side-task verifier under ``name`` (used as a decorator)."""

    def deco(fn: V) -> V:
        if name in VERIFIER_REGISTRY and VERIFIER_REGISTRY[name] is not fn:
            raise ValueError(f"verifier {name!r} is already registered to a different callable")
        VERIFIER_REGISTRY[name] = fn
        return fn

    return deco


def get_scorer(name: str) -> MainTaskScorer:
    """Return the registered scorer, or raise ``RegistryError`` with the known names."""
    if name not in SCORER_REGISTRY:
        raise RegistryError(f"unknown scorer {name!r}; registered: {sorted(SCORER_REGISTRY)}")
    return SCORER_REGISTRY[name]


def get_main_task_scorer(config: RunConfig) -> MainTaskScorer:
    """The scorer the run config names (``main_task.scorer``)."""
    return get_scorer(str(config.main_task["scorer"]))


def get_verifier(name: str) -> _Verifier:
    """Return the registered verifier, or raise ``RegistryError`` with the known names."""
    if name not in VERIFIER_REGISTRY:
        raise RegistryError(f"unknown verifier {name!r}; registered: {sorted(VERIFIER_REGISTRY)}")
    return VERIFIER_REGISTRY[name]


def is_scorer(name: str) -> bool:
    """Whether a scorer is registered under ``name``."""
    return name in SCORER_REGISTRY


def is_verifier(name: str) -> bool:
    """Whether a verifier is registered under ``name``."""
    return name in VERIFIER_REGISTRY
