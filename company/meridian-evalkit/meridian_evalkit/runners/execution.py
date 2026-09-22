"""Concrete sequential and parallel eval runners.

:class:`SequentialRunner` drives items through the serving engine on a single lane. :class:`ParallelRunner`
spreads them across several deterministic lanes. Both merge their per-item predictions back into the input
order, so they return equal results for the same items and engine; the only difference is the order the
scheduler visits items.
"""

from __future__ import annotations

from meridian_evalkit.runners.base import EngineRunner
from meridian_serving.api.serve import ServingEngine


class SequentialRunner(EngineRunner):
    """A single-lane runner: items are served in their given order."""

    def __init__(self, engine: ServingEngine | None = None, *, max_tokens: int = 16) -> None:
        """Wire the runner to a serving engine and a per-item generation budget."""
        super().__init__(engine, num_lanes=1, max_tokens=max_tokens)


class ParallelRunner(EngineRunner):
    """A multi-lane runner: items are spread deterministically across ``num_lanes`` lanes."""

    def __init__(
        self,
        engine: ServingEngine | None = None,
        *,
        num_lanes: int = 4,
        max_tokens: int = 16,
    ) -> None:
        """Wire the runner to a serving engine, a lane count, and a per-item generation budget."""
        super().__init__(engine, num_lanes=num_lanes, max_tokens=max_tokens)
