"""A deterministic item scheduler that models concurrency without real threads.

:class:`ItemScheduler` assigns items to a fixed number of lanes in round-robin order and produces a stable
execution order over ``(lane, item_index)`` pairs. Because the assignment is a pure function of the item count
and the lane count, a "parallel" run over several lanes visits items in a reproducible order and merges its
per-item outputs back into the original item order, so it is byte-identical to a single-lane run.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_evalkit.errors import RunnerError


@dataclass(frozen=True)
class LaneAssignment:
    """One scheduled unit of work: the lane it runs on and the index of the item it processes."""

    lane: int
    item_index: int


class ItemScheduler:
    """Assigns item indices to ``num_lanes`` lanes round-robin and orders the resulting work."""

    def __init__(self, num_lanes: int = 1) -> None:
        """Hold the lane count (at least one)."""
        if num_lanes < 1:
            raise RunnerError("num_lanes must be >= 1", code="evalkit.runner", num_lanes=num_lanes)
        self._num_lanes = num_lanes

    @property
    def num_lanes(self) -> int:
        """The number of lanes work is spread across."""
        return self._num_lanes

    def assign(self, num_items: int) -> list[LaneAssignment]:
        """Assign ``num_items`` item indices to lanes round-robin, one assignment per item."""
        if num_items < 0:
            raise RunnerError("num_items must be non-negative", code="evalkit.runner")
        return [LaneAssignment(lane=i % self._num_lanes, item_index=i) for i in range(num_items)]

    def execution_order(self, num_items: int) -> list[int]:
        """The item indices in execution order: lane 0's items first, then lane 1's, and so on.

        Within a lane the items keep their original relative order, so draining the lanes in order gives a
        stable, reproducible traversal of every item exactly once.
        """
        by_lane: list[list[int]] = [[] for _ in range(self._num_lanes)]
        for assignment in self.assign(num_items):
            by_lane[assignment.lane].append(assignment.item_index)
        order: list[int] = []
        for lane_items in by_lane:
            order.extend(lane_items)
        return order
