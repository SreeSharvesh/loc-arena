"""Runners: drive eval items through the serving stack over a deterministic lane schedule.

Owns the deterministic :class:`ItemScheduler`, the :class:`EngineRunner` that turns items into predictions
through a serving engine, and the :class:`SequentialRunner`/:class:`ParallelRunner` façades.
"""

from __future__ import annotations

from meridian_evalkit.runners.base import EngineRunner, RunnerResult
from meridian_evalkit.runners.execution import ParallelRunner, SequentialRunner
from meridian_evalkit.runners.scheduler import ItemScheduler, LaneAssignment

__all__ = [
    "EngineRunner",
    "ItemScheduler",
    "LaneAssignment",
    "ParallelRunner",
    "RunnerResult",
    "SequentialRunner",
]
