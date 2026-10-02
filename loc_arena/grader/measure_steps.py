"""The measure steps scenarios register by scorer name; see docs/isolation/design.md#grader."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loc_arena.execution.checkout import extract_last_line
from loc_arena.stack.contracts import ContractModel
from loc_arena.stack.settings import GradingSettings


@dataclass(frozen=True)
class MeasurementRequest:
    """What a measure step measures: the checkout, the harness files, and the trusted repositories."""

    checkout: Path
    harness_directory: Path
    repositories: tuple[str, ...]
    settings: GradingSettings


@dataclass(frozen=True)
class MeasureStep[M: ContractModel]:
    """One scorer's measure step: its name, its measurement's wire type, how it measures, what failure is."""

    name: str
    measurement_type: type[M]
    measure: Callable[[MeasurementRequest], M]
    failed_measurement: M

    def measure_as_json(self, request: MeasurementRequest) -> str:
        """Measure ``request`` and return the measurement as the one JSON line the grader prints."""
        return self.measure(request).model_dump_json()

    def parse(self, output: bytes, settings: GradingSettings) -> M:
        """The measurement in a grader's output, read as untrusted: size-capped, last line, strict types."""
        if len(output) > settings.max_output_bytes:
            cap = settings.max_output_bytes
            raise ValueError(f"the grader printed {len(output)} bytes, over the {cap} cap")
        return self.measurement_type.model_validate_json(
            extract_last_line(output.decode(errors="replace")),
            strict=True,
        )


MEASURE_STEPS: dict[str, MeasureStep[ContractModel]] = {}


def register_measure_step[M: ContractModel](step: MeasureStep[M]) -> MeasureStep[M]:
    """Register ``step`` under its name and return it; a different step under the same name is an error."""
    registered = MEASURE_STEPS.get(step.name)
    if registered is not None and registered is not step:
        raise ValueError(f"measure step {step.name!r} is already registered to a different step")
    MEASURE_STEPS[step.name] = step
    return step


def get_measure_step(name: str) -> MeasureStep[ContractModel]:
    """The measure step registered under ``name``; raises ``KeyError`` listing the known."""
    if name not in MEASURE_STEPS:
        raise KeyError(f"no measure step {name!r}; registered: {sorted(MEASURE_STEPS)}")
    return MEASURE_STEPS[name]


def load_measure_module(path: Path) -> None:
    """Import a scenario's ``measure.py`` from ``path``, which registers its measure steps."""
    spec = importlib.util.spec_from_file_location("scenario_measure", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a measure module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
