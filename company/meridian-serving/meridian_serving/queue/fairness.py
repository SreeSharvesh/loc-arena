"""Fair queueing across tenants via deficit round robin.

A shared serving cluster must not let one noisy tenant starve the others. Deficit round robin gives each
tenant
a per-round token quantum; a tenant accrues deficit it can spend on its queued requests, and unspent deficit
carries over, so over time every tenant gets its fair share of serving tokens regardless of request size. The
scheduler is deterministic given the arrival order.
"""

from __future__ import annotations

from collections import OrderedDict, deque

from meridian_serving.types import Request


class DeficitRoundRobin:
    """Fair-shares serving tokens across tenants with a per-round quantum and carried-over deficit."""

    def __init__(self, quantum: int = 256) -> None:
        """Hold the per-round token quantum granted to each active tenant."""
        if quantum < 1:
            raise ValueError("quantum must be >= 1")
        self._quantum = quantum
        self._queues: OrderedDict[str, deque[Request]] = OrderedDict()
        self._deficit: dict[str, int] = {}

    def enqueue(self, tenant: str, request: Request) -> None:
        """Add ``request`` to ``tenant``'s queue (creating it if new)."""
        if tenant not in self._queues:
            self._queues[tenant] = deque()
            self._deficit[tenant] = 0
        self._queues[tenant].append(request)

    def _active(self) -> list[str]:
        return [t for t, q in self._queues.items() if q]

    def dispatch_round(self) -> list[Request]:
        """Run one deficit round: grant each active tenant a quantum and dispatch what its deficit affords."""
        dispatched: list[Request] = []
        for tenant in self._active():
            self._deficit[tenant] += self._quantum
            queue = self._queues[tenant]
            while queue and queue[0].total_len <= self._deficit[tenant]:
                req = queue.popleft()
                self._deficit[tenant] -= req.total_len
                dispatched.append(req)
            if not queue:
                self._deficit[tenant] = 0  # idle tenants do not bank deficit
        return dispatched

    def drain(self, max_rounds: int = 1000) -> list[Request]:
        """Run rounds until every tenant's queue is empty (or the round cap is hit)."""
        out: list[Request] = []
        rounds = 0
        while self._active() and rounds < max_rounds:
            out.extend(self.dispatch_round())
            rounds += 1
        return out

    def pending(self) -> int:
        """The total number of still-queued requests across tenants."""
        return sum(len(q) for q in self._queues.values())
