"""The throughput/cost benchmark: run a workload through serving and report its cost.

:class:`ThroughputBench` drives a workload through a
:class:`meridian_serving.api.serve.ServingEngine` and reports the scheduling cost (the total padded token
slots the serving step occupies), the served checksum, the generated-token total, and the derived cost per
generated token. The scheduling cost is exactly what serving's scheduler produces for the workload, so a
change to serving's batching moves the benchmark value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from meridian_evalkit.errors import BenchError
from meridian_serving.api.serve import ServingEngine
from meridian_serving.types import Request


@dataclass(frozen=True)
class BenchResult:
    """A benchmark outcome: the workload size, the serving cost, and derived throughput figures."""

    num_requests: int
    admitted: int
    rejected: int
    schedule_cost: int
    served_checksum: int
    total_generated: int

    @property
    def cost_per_generated_token(self) -> float:
        """The scheduling cost divided by the generated-token total (``0.0`` if nothing was generated)."""
        if self.total_generated == 0:
            return 0.0
        return self.schedule_cost / self.total_generated

    @property
    def cost_per_request(self) -> float:
        """The scheduling cost divided by the admitted-request count (``0.0`` if none were admitted)."""
        if self.admitted == 0:
            return 0.0
        return self.schedule_cost / self.admitted


class ThroughputBench:
    """Runs a workload through a serving engine and reports its throughput and cost."""

    def __init__(self, engine: ServingEngine | None = None) -> None:
        """Wire the bench to a serving engine (a default engine if none is given)."""
        self._engine = engine if engine is not None else ServingEngine()

    @property
    def engine(self) -> ServingEngine:
        """The serving engine this bench drives."""
        return self._engine

    def run(self, workload: Sequence[Request]) -> BenchResult:
        """Serve ``workload`` through the engine and report its cost and throughput."""
        if not workload:
            raise BenchError("bench needs a non-empty workload", code="evalkit.bench")
        result = self._engine.serve(list(workload))
        return BenchResult(
            num_requests=len(workload),
            admitted=result.admitted,
            rejected=result.rejected,
            schedule_cost=result.schedule_cost,
            served_checksum=result.served_checksum,
            total_generated=result.total_generated,
        )
