"""The benchmark harness: run items through a model, score them, and compute teacher features.

:class:`Harness` drives a :class:`~meridian_evalkit.harness.model.Model` over a list of
:class:`~meridian_evalkit.harness.model.EvalItem`, turning each answer into a
:class:`~meridian_evalkit.metrics.records.ScoreRecord` and a teacher-feature vector (via the distill feature
surface). It reduces the records through a :class:`~meridian_evalkit.metrics.registry.MetricRegistry` into a
:class:`HarnessReport`. Scoring is exact-match on the reference tokens; everything is deterministic given the
model and items.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from meridian_evalkit.errors import HarnessError
from meridian_evalkit.harness.features import item_features
from meridian_evalkit.harness.model import EvalItem, Model, Prediction
from meridian_evalkit.metrics.records import ScoreRecord
from meridian_evalkit.metrics.registry import MetricRegistry, default_registry
from meridian_evalkit.metrics.stratified import stratified


@dataclass(frozen=True)
class ItemResult:
    """One scored item: its id, category, correctness, confidence, prediction, and teacher features."""

    item_id: str
    category: str
    correct: bool
    confidence: float
    prediction: tuple[int, ...]
    reference: tuple[int, ...]
    features: tuple[float, ...]

    def to_record(self) -> ScoreRecord:
        """The :class:`ScoreRecord` this result contributes to the metrics."""
        return ScoreRecord(
            item_id=self.item_id,
            correct=self.correct,
            confidence=self.confidence,
            category=self.category,
            prediction=self.prediction,
            reference=self.reference,
        )


@dataclass(frozen=True)
class HarnessReport:
    """A harness run: the per-item results and the reduced metric values."""

    results: tuple[ItemResult, ...]
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def size(self) -> int:
        """The number of items scored."""
        return len(self.results)

    def records(self) -> list[ScoreRecord]:
        """The score records for every item, in scoring order."""
        return [r.to_record() for r in self.results]

    def features(self) -> dict[str, tuple[float, ...]]:
        """The teacher-feature vector for each item, keyed by item id."""
        return {r.item_id: r.features for r in self.results}

    def accuracy_by_category(self) -> dict[str, float]:
        """The per-category accuracy across the scored items."""
        return stratified(self.records())


class Harness:
    """Runs a model over eval items and reduces the outcome through a metric registry."""

    def __init__(self, registry: MetricRegistry | None = None) -> None:
        """Use the given metric registry, or the built-in default set."""
        self._registry = registry if registry is not None else default_registry()

    @property
    def registry(self) -> MetricRegistry:
        """The metric registry this harness reduces through."""
        return self._registry

    def score_item(self, item: EvalItem, prediction: Prediction) -> ItemResult:
        """Score one ``prediction`` against ``item`` and compute its teacher features."""
        if prediction.item_id != item.item_id:
            raise HarnessError(
                "prediction does not match item",
                code="evalkit.harness",
                item_id=item.item_id,
                prediction_id=prediction.item_id,
            )
        correct = prediction.tokens == item.reference
        return ItemResult(
            item_id=item.item_id,
            category=item.category,
            correct=correct,
            confidence=prediction.confidence,
            prediction=prediction.tokens,
            reference=item.reference,
            features=item_features(item, prediction),
        )

    def run(self, model: Model, items: Sequence[EvalItem]) -> HarnessReport:
        """Run ``model`` over ``items``, score each, and reduce the records into a report."""
        if not items:
            raise HarnessError("harness needs at least one item", code="evalkit.harness")
        results = [self.score_item(item, model.predict(item)) for item in items]
        records = [r.to_record() for r in results]
        metrics = self._registry.evaluate_all(records)
        return HarnessReport(results=tuple(results), metrics=metrics)
