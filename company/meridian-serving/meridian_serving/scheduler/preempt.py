"""Preemptive priority scheduling with swap-out under memory pressure.

When the running set exceeds the KV-memory budget, the scheduler preempts the lowest-priority running requests
(swapping their state out) to admit higher-priority waiting ones, and later swaps preempted requests back in
when memory frees up. This keeps high-priority latency low under load. The scheduler is deterministic:
priority
then arrival order fully determine every decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian_serving.types import Request


@dataclass
class RunningState:
    """The set of currently-running and swapped-out requests plus the memory each consumes."""

    running: list[Request] = field(default_factory=list)
    swapped: list[Request] = field(default_factory=list)

    def used_tokens(self) -> int:
        """The KV tokens the running set consumes."""
        return sum(r.total_len for r in self.running)


@dataclass(frozen=True)
class SchedulerDecision:
    """One scheduling pass: which waiting requests were admitted, which running ones were preempted."""

    admitted: tuple[Request, ...]
    preempted: tuple[Request, ...]
    resumed: tuple[Request, ...]


class PreemptiveScheduler:
    """Admits, preempts, and resumes requests to keep the running set within a KV token budget."""

    def __init__(self, memory_tokens: int) -> None:
        """Hold the KV token budget for the running set."""
        if memory_tokens < 1:
            raise ValueError("memory_tokens must be >= 1")
        self._budget = memory_tokens

    def _priority_key(self, request: Request) -> tuple[int, int]:
        return (int(request.priority), -request.arrival_seq)

    def schedule(self, state: RunningState, waiting: list[Request]) -> SchedulerDecision:
        """Admit as many waiting requests as fit, preempting lower-priority running ones if that helps."""
        admitted: list[Request] = []
        preempted: list[Request] = []
        resumed: list[Request] = []

        # first, resume swapped requests if there is room (highest priority first)
        for req in sorted(state.swapped, key=self._priority_key, reverse=True):
            if state.used_tokens() + req.total_len <= self._budget:
                state.swapped.remove(req)
                state.running.append(req)
                resumed.append(req)

        for req in sorted(waiting, key=self._priority_key, reverse=True):
            if state.used_tokens() + req.total_len <= self._budget:
                state.running.append(req)
                admitted.append(req)
                continue
            # try to make room by preempting strictly lower-priority running requests
            victims = sorted(
                (r for r in state.running if int(r.priority) < int(req.priority)),
                key=self._priority_key,
            )
            freed = 0
            chosen: list[Request] = []
            need = state.used_tokens() + req.total_len - self._budget
            for victim in victims:
                if freed >= need:
                    break
                chosen.append(victim)
                freed += victim.total_len
            if freed >= need:
                for victim in chosen:
                    state.running.remove(victim)
                    state.swapped.append(victim)
                    preempted.append(victim)
                state.running.append(req)
                admitted.append(req)

        return SchedulerDecision(admitted=tuple(admitted), preempted=tuple(preempted), resumed=tuple(resumed))
