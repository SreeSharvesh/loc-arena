"""A priority request queue with FIFO tie-breaking.

Requests are ordered by priority (higher first) and, within a priority, by arrival sequence (earlier first).
The queue is bounded; pushing past ``capacity`` raises :class:`~meridian_serving.errors.QueueFullError`.
``pop`` and ``peek`` are deterministic given the arrival order, so scheduling is reproducible.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from meridian_serving.errors import QueueFullError
from meridian_serving.types import Priority, Request


@dataclass(order=True)
class _Entry:
    # heap orders by (−priority, arrival_seq); the request itself is not compared
    sort_priority: int
    arrival_seq: int
    request: Request = field(compare=False)


class RequestQueue:
    """A bounded max-priority queue over :class:`Request` with FIFO tie-breaking."""

    def __init__(self, capacity: int = 1024) -> None:
        """Hold the capacity and an empty heap; assign a monotonic arrival sequence to each push."""
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._heap: list[_Entry] = []
        self._seq = 0

    def __len__(self) -> int:
        """The number of queued requests."""
        return len(self._heap)

    @property
    def capacity(self) -> int:
        """The maximum number of requests the queue holds."""
        return self._capacity

    def is_full(self) -> bool:
        """Whether the queue is at capacity."""
        return len(self._heap) >= self._capacity

    def push(self, request: Request) -> Request:
        """Enqueue ``request`` (stamping its arrival sequence); raise if the queue is full."""
        if self.is_full():
            raise QueueFullError("request queue is full", capacity=self._capacity)
        stamped = Request(
            request_id=request.request_id,
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            priority=request.priority,
            arrival_seq=self._seq,
        )
        heapq.heappush(self._heap, _Entry(-int(stamped.priority), self._seq, stamped))
        self._seq += 1
        return stamped

    def pop(self) -> Request:
        """Remove and return the highest-priority, earliest request; raise ``IndexError`` if empty."""
        if not self._heap:
            raise IndexError("pop from an empty request queue")
        return heapq.heappop(self._heap).request

    def peek(self) -> Request:
        """Return the next request without removing it; raise ``IndexError`` if empty."""
        if not self._heap:
            raise IndexError("peek at an empty request queue")
        return self._heap[0].request

    def drain(self, n: int) -> list[Request]:
        """Pop up to ``n`` requests in priority order."""
        return [self.pop() for _ in range(min(n, len(self._heap)))]

    def snapshot(self) -> list[Request]:
        """The queued requests in pop order (does not mutate the queue)."""
        return [entry.request for entry in sorted(self._heap)]

    def counts_by_priority(self) -> dict[Priority, int]:
        """How many queued requests are at each priority."""
        counts: dict[Priority, int] = {p: 0 for p in Priority}
        for entry in self._heap:
            counts[entry.request.priority] += 1
        return counts
