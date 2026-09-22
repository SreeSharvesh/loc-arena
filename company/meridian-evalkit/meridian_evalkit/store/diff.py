"""Diffing two stored eval runs.

:class:`RunDiff` compares two :class:`~meridian_evalkit.store.models.EvalRun` records and reports the metric
deltas (for metrics present in both), the metric names added or removed between them, and the item ids added
or removed from the scored set. :func:`diff_runs` builds one. The diff is deterministic and order-independent.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_evalkit.store.models import EvalRun


@dataclass(frozen=True)
class MetricDelta:
    """One metric's change between two runs: the old value, the new value, and their difference."""

    name: str
    old: float
    new: float

    @property
    def delta(self) -> float:
        """The signed change ``new - old``."""
        return self.new - self.old


@dataclass(frozen=True)
class RunDiff:
    """The difference between two runs: metric deltas and the added/removed metrics and items."""

    left: str
    right: str
    metric_deltas: tuple[MetricDelta, ...]
    added_metrics: tuple[str, ...]
    removed_metrics: tuple[str, ...]
    added_items: tuple[str, ...]
    removed_items: tuple[str, ...]

    @property
    def unchanged(self) -> bool:
        """Whether the two runs have identical metrics and scored item sets."""
        return (
            not self.added_metrics
            and not self.removed_metrics
            and not self.added_items
            and not self.removed_items
            and all(d.delta == 0.0 for d in self.metric_deltas)
        )

    def delta(self, name: str) -> float:
        """The signed change of metric ``name`` (raises :class:`KeyError` if it is not in both runs)."""
        for d in self.metric_deltas:
            if d.name == name:
                return d.delta
        raise KeyError(name)


def diff_runs(left: EvalRun, right: EvalRun) -> RunDiff:
    """Compare ``left`` to ``right`` and report metric deltas plus added/removed metrics and items."""
    left_metrics = set(left.metrics)
    right_metrics = set(right.metrics)
    shared = sorted(left_metrics & right_metrics)
    deltas = tuple(MetricDelta(name=name, old=left.metrics[name], new=right.metrics[name]) for name in shared)
    left_items = set(left.item_ids)
    right_items = set(right.item_ids)
    return RunDiff(
        left=left.run_id,
        right=right.run_id,
        metric_deltas=deltas,
        added_metrics=tuple(sorted(right_metrics - left_metrics)),
        removed_metrics=tuple(sorted(left_metrics - right_metrics)),
        added_items=tuple(sorted(right_items - left_items)),
        removed_items=tuple(sorted(left_items - right_items)),
    )
