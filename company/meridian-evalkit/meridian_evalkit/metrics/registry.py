"""A registry of named metrics over scored records.

:class:`MetricRegistry` maps a metric name to a callable that reduces a list of
:class:`~meridian_evalkit.metrics.records.ScoreRecord` to a float, and remembers whether higher is better so
downstream ranking knows the orientation. :func:`default_registry` wires the built-in metrics (accuracy,
error rate, ECE, Brier, and the reference-embedding similarity); ``pass@k`` is registered as a family keyed by
``k`` through :meth:`MetricRegistry.register`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics.calibration import brier_score, expected_calibration_error
from meridian_evalkit.metrics.classification import accuracy, error_rate, pass_at_k
from meridian_evalkit.metrics.embedding import reference_embedding_similarity
from meridian_evalkit.metrics.records import ScoreRecord

MetricFn = Callable[[Sequence[ScoreRecord]], float]


@dataclass(frozen=True)
class Metric:
    """A named metric: its reducing function and whether a higher value is better."""

    name: str
    fn: MetricFn
    higher_is_better: bool

    def __call__(self, records: Sequence[ScoreRecord]) -> float:
        """Reduce ``records`` to this metric's value."""
        return self.fn(records)


class MetricRegistry:
    """Owns named metrics and evaluates them over a record list."""

    def __init__(self) -> None:
        """Start empty."""
        self._metrics: dict[str, Metric] = {}

    def register(self, name: str, fn: MetricFn, *, higher_is_better: bool = True) -> None:
        """Register ``fn`` under ``name`` (raises on a duplicate name)."""
        if name in self._metrics:
            raise MetricError("metric already registered", code="evalkit.metric", name=name)
        self._metrics[name] = Metric(name=name, fn=fn, higher_is_better=higher_is_better)

    def get(self, name: str) -> Metric:
        """The metric registered under ``name`` (raises if unknown)."""
        try:
            return self._metrics[name]
        except KeyError as exc:
            raise MetricError("unknown metric", code="evalkit.metric", name=name) from exc

    def names(self) -> tuple[str, ...]:
        """The registered metric names, sorted."""
        return tuple(sorted(self._metrics))

    def compute(self, name: str, records: Sequence[ScoreRecord]) -> float:
        """Evaluate the metric ``name`` over ``records``."""
        return self.get(name)(records)

    def evaluate_all(self, records: Sequence[ScoreRecord]) -> dict[str, float]:
        """Evaluate every registered metric over ``records`` into a name -> value mapping."""
        return {name: self._metrics[name](records) for name in self.names()}


def _typed(fn: Callable[..., float], **kwargs: Any) -> MetricFn:
    """Bind keyword arguments onto a metric callable, preserving the record-sequence signature."""
    return partial(fn, **kwargs)


def default_registry(*, ece_bins: int = 10, pass_k: Sequence[int] = (1,)) -> MetricRegistry:
    """A registry wired with the built-in metrics plus a ``pass@k`` entry for each requested ``k``."""
    registry = MetricRegistry()
    registry.register("accuracy", accuracy, higher_is_better=True)
    registry.register("error_rate", error_rate, higher_is_better=False)
    registry.register("ece", _typed(expected_calibration_error, n_bins=ece_bins), higher_is_better=False)
    registry.register("brier", brier_score, higher_is_better=False)
    registry.register("reference_embedding", reference_embedding_similarity, higher_is_better=True)
    for k in pass_k:
        registry.register(f"pass@{k}", _typed(pass_at_k, k=k), higher_is_better=True)
    return registry
