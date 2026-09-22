"""Store: an append-only, versioned results store with run diffing.

Owns the :class:`EvalRun` record, the :class:`ResultsStore` that versions runs, and the :class:`RunDiff`
comparison between two stored versions.
"""

from __future__ import annotations

from meridian_evalkit.store.diff import MetricDelta, RunDiff, diff_runs
from meridian_evalkit.store.models import EvalRun
from meridian_evalkit.store.store import ResultsStore

__all__ = [
    "EvalRun",
    "MetricDelta",
    "ResultsStore",
    "RunDiff",
    "diff_runs",
]
