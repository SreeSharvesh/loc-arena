"""The batch scheduler: form batches from queued requests and account their padded cost.

The scheduler groups requests into batches of at most ``batch_size`` in priority/arrival order. Each batch is
padded to a common width so the requests in it run as one rectangular tensor. The width the scheduler chooses
determines how many padded token-slots the batch occupies, which is the dominant cost of a serving step.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common import cost
from meridian_serving.types import Batch, Request


@dataclass(frozen=True)
class SchedulePlan:
    """The batches formed for one scheduling pass plus their total padded cost."""

    batches: tuple[Batch, ...]

    @property
    def total_padded_tokens(self) -> int:
        """The total padded token-slots across all batches (the serving-step cost)."""
        return sum(b.padded_tokens for b in self.batches)

    @property
    def total_real_tokens(self) -> int:
        """The total real (unpadded) prompt tokens across all batches."""
        return sum(b.real_tokens for b in self.batches)

    @property
    def padding_waste(self) -> int:
        """The padded token-slots that carry no real token (pure overhead)."""
        return self.total_padded_tokens - self.total_real_tokens


class BatchScheduler:
    """Forms padded batches of at most ``batch_size`` requests."""

    def __init__(self, batch_size: int = 8) -> None:
        """Hold the maximum batch size."""
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._batch_size = batch_size

    @property
    def batch_size(self) -> int:
        """The maximum number of requests per batch."""
        return self._batch_size

    def form_batches(self, requests: list[Request]) -> SchedulePlan:
        """Group ``requests`` (already in scheduling order) into padded batches and return the plan.

        The padding width applied to every batch is the maximum prompt length across the whole scheduling
        set, so the batches run as uniform rectangular tensors of a single width.
        """
        if not requests:
            return SchedulePlan(batches=())
        width = max(r.prompt_len for r in requests)
        batches: list[Batch] = []
        for start in range(0, len(requests), self._batch_size):
            group = tuple(requests[start : start + self._batch_size])
            batches.append(Batch(requests=group, padded_len=width))
        plan = SchedulePlan(batches=tuple(batches))
        cost.record("serving.padded_tokens", plan.total_padded_tokens)  # padded token-slots this pass
        return plan

    def step_cost(self, requests: list[Request]) -> int:
        """The padded-token cost of scheduling ``requests`` in one pass."""
        return self.form_batches(requests).total_padded_tokens


class ContinuousBatcher:
    """Fills a running batch up to a token budget, admitting requests until the budget is reached."""

    def __init__(self, token_budget: int, *, max_batch: int = 32) -> None:
        """Hold the per-step token budget and the hard cap on batch size."""
        if token_budget < 1:
            raise ValueError("token_budget must be >= 1")
        self._budget = token_budget
        self._max_batch = max_batch

    def fill(self, requests: list[Request]) -> tuple[list[Request], list[Request]]:
        """Split ``requests`` into (admitted this step, deferred) under the token budget and batch cap.

        Admission is greedy in the given order; a request that would push the running padded cost over the
        budget is deferred to the next step, along with everything after it.
        """
        admitted: list[Request] = []
        used = 0
        width = 0
        for i, req in enumerate(requests):
            candidate_width = max(width, req.prompt_len)
            candidate_cost = candidate_width * (len(admitted) + 1)
            if admitted and (candidate_cost > self._budget or len(admitted) >= self._max_batch):
                return admitted, requests[i:]
            admitted.append(req)
            width = candidate_width
            used = candidate_cost
        _ = used
        return admitted, []
