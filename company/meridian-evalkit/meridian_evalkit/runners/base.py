"""Serving-driven eval runners: turn eval items into predictions through the serving stack.

An :class:`EngineRunner` maps every :class:`~meridian_evalkit.harness.model.EvalItem` to a
:class:`meridian_serving.types.Request`, drives them through a
:class:`meridian_serving.api.serve.ServingEngine`, and maps each response back into a
:class:`~meridian_evalkit.harness.model.Prediction`. Items are visited in the order chosen by a deterministic
:class:`~meridian_evalkit.runners.scheduler.ItemScheduler`, and results are merged back into the input order,
so a multi-lane runner produces the same :class:`RunnerResult` as a single-lane one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from meridian_evalkit.errors import RunnerError
from meridian_evalkit.harness.model import EvalItem, Prediction
from meridian_evalkit.runners.scheduler import ItemScheduler
from meridian_serving.api.serve import ServingEngine
from meridian_serving.types import Request, Response


@dataclass(frozen=True)
class RunnerResult:
    """A runner pass: the predictions in item order plus the serving cost accounting."""

    predictions: tuple[Prediction, ...]
    schedule_cost: int
    served_checksum: int
    admitted: int
    rejected: int
    execution_order: tuple[str, ...]

    @property
    def size(self) -> int:
        """The number of predictions produced."""
        return len(self.predictions)


def _confidence(tokens: tuple[int, ...]) -> float:
    """A deterministic confidence in ``[0, 1]`` derived from a response's tokens."""
    if not tokens:
        return 0.0
    acc = 0
    for tok in tokens:
        acc = (acc * 131 + tok + 1) & 0xFFFFFFFF
    return round((acc % 1000) / 1000.0, 12)


class EngineRunner:
    """Drives eval items through a serving engine over a deterministic lane schedule."""

    def __init__(
        self,
        engine: ServingEngine | None = None,
        *,
        num_lanes: int = 1,
        max_tokens: int = 16,
    ) -> None:
        """Wire the runner to a serving engine, a lane count, and a per-item generation budget."""
        if max_tokens < 1:
            raise RunnerError("max_tokens must be >= 1", code="evalkit.runner", max_tokens=max_tokens)
        self._engine = engine if engine is not None else ServingEngine()
        self._scheduler = ItemScheduler(num_lanes)
        self._max_tokens = max_tokens

    @property
    def num_lanes(self) -> int:
        """The number of scheduling lanes this runner spreads items across."""
        return self._scheduler.num_lanes

    def _request(self, item: EvalItem, seq: int) -> Request:
        """Build the serving request for ``item`` with a stable arrival order ``seq``."""
        return Request(
            request_id=item.item_id,
            prompt=item.prompt,
            max_tokens=self._max_tokens,
            arrival_seq=seq,
        )

    def run(self, items: Sequence[EvalItem]) -> RunnerResult:
        """Serve ``items`` through the engine in lane-scheduled order and merge predictions to item order."""
        if not items:
            raise RunnerError("runner needs at least one item", code="evalkit.runner")
        order = self._scheduler.execution_order(len(items))
        requests = [self._request(items[idx], seq) for seq, idx in enumerate(order)]
        result = self._engine.serve(requests)
        by_id: dict[str, Response] = {r.request_id: r for r in result.responses}
        predictions: list[Prediction] = []
        for item in items:
            response = by_id.get(item.item_id)
            if response is None:
                raise RunnerError(
                    "serving admitted no response for item",
                    code="evalkit.runner",
                    item_id=item.item_id,
                )
            predictions.append(
                Prediction(
                    item_id=item.item_id,
                    tokens=response.tokens,
                    confidence=_confidence(response.tokens),
                ),
            )
        return RunnerResult(
            predictions=tuple(predictions),
            schedule_cost=result.schedule_cost,
            served_checksum=result.served_checksum,
            admitted=result.admitted,
            rejected=result.rejected,
            execution_order=tuple(items[idx].item_id for idx in order),
        )
