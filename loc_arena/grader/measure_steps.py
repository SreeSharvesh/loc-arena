"""The measure steps scenarios register by scorer name: the half of a main-task grade the grader runs.

A scenario's ``measure.py`` builds a :class:`MeasureStep` (its measurement type, the function that measures a
checkout, and the measurement an unusable grader output stands for) and registers it under the name its run
config gives ``main_task.scorer``. The host pairs it with the scenario's score step
(:class:`loc_arena.registry.ScorerSteps`).

In the stack, the grader container (``sandbox`` image, no network) receives the scenario's ``measure.py`` by
a read-only bind mount, loads it with :func:`load_measure_module` and runs the step named by the run's scorer;
the host reads what it printed as untrusted (:meth:`MeasureStep.parse`). So a ``measure.py`` imports only what
the sandbox image ships: :mod:`loc_arena.grader`, :mod:`loc_arena.stack`, :mod:`loc_arena.execution`, the
standard library and third-party packages (``tests/unit/test_sandbox_image.py`` loads every scenario's module
the same way).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loc_arena.execution.checkout import extract_last_line
from loc_arena.stack.contracts import ContractModel
from loc_arena.stack.settings import GradingSettings

# The name a scenario's measure module is imported under when loaded from a file (the grader's mounted copy).
SCENARIO_MEASURE_MODULE_NAME: Final = "scenario_measure"


@dataclass(frozen=True)
class MeasurementRequest:
    """What a measure step measures: the checkout, the harness files, and the repositories to trust.

    ``repositories`` is the pristine list (from the codebase the checkout was seeded from), never the
    checkout's own: agent code may have added or removed directories there.
    """

    checkout: Path
    harness_directory: Path
    repositories: tuple[str, ...]
    settings: GradingSettings
    python_executable: str = sys.executable


@dataclass(frozen=True)
class MeasureStep[M: ContractModel]:
    """One scorer's measure step: its name, its measurement's wire type, how it measures, what failure is.

    ``failed_measurement`` is what the host scores when the grader printed no valid measurement (it hung, it
    crashed, agent code flooded its output): a broken outcome, never a crash of the harness.
    """

    name: str
    measurement_type: type[M]
    measure: Callable[[MeasurementRequest], M]
    failed_measurement: M

    def measure_as_json(self, request: MeasurementRequest) -> str:
        """Measure ``request`` and return the measurement as the one JSON line the grader prints."""
        return self.measure(request).model_dump_json()

    def parse(self, output: bytes, settings: GradingSettings) -> M:
        """The measurement in a grader's output, read as untrusted: size-capped, last line, strict types.

        Raises ``ValueError`` (pydantic's ``ValidationError`` included) when the output exceeds
        ``max_output_bytes`` or its last line is not exactly a measurement of this step's type.
        """
        if len(output) > settings.max_output_bytes:
            cap = settings.max_output_bytes
            raise ValueError(f"the grader printed {len(output)} bytes, over the {cap} cap")
        return self.measurement_type.model_validate_json(
            extract_last_line(output.decode(errors="replace")),
            strict=True,
        )


class UnknownMeasureStepError(KeyError):
    """No measure step is registered under the requested scorer name."""


MEASURE_STEPS: dict[str, MeasureStep[ContractModel]] = {}


def register_measure_step[M: ContractModel](step: MeasureStep[M]) -> MeasureStep[M]:
    """Register ``step`` under its name and return it; a different step under the same name is an error."""
    registered = MEASURE_STEPS.get(step.name)
    if registered is not None and registered is not step:
        raise ValueError(f"measure step {step.name!r} is already registered to a different step")
    MEASURE_STEPS[step.name] = step
    return step


def get_measure_step(name: str) -> MeasureStep[ContractModel]:
    """The measure step registered under ``name``; raises ``UnknownMeasureStepError`` listing the known."""
    if name not in MEASURE_STEPS:
        raise UnknownMeasureStepError(f"no measure step {name!r}; registered: {sorted(MEASURE_STEPS)}")
    return MEASURE_STEPS[name]


def load_measure_module(path: Path, module_name: str = SCENARIO_MEASURE_MODULE_NAME) -> None:
    """Import a scenario's ``measure.py`` from ``path``, which registers its measure steps.

    Imported once per process: a second call under the same module name does nothing, since re-running the
    module would register new step objects under names already taken.
    """
    if module_name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a measure module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)  # a failed import must not look loaded to the next call
        raise
