"""Chunked prefill: split a long prompt's prefill into fixed-size chunks.

A very long prompt would otherwise monopolize a serving step. Chunked prefill breaks the prompt into chunks of
at most ``chunk_size`` tokens and interleaves them across steps, so short decode requests are not starved
behind
a single long prefill. The planner reports, per step, how many prompt tokens are processed and how much
prefill
remains.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_serving.types import Request


@dataclass(frozen=True)
class PrefillChunk:
    """One chunk of a request's prefill: the request id, the token span, and whether it completes prefill."""

    request_id: str
    start: int
    end: int
    is_last: bool

    @property
    def length(self) -> int:
        """The number of tokens in this chunk."""
        return self.end - self.start


class ChunkedPrefillPlanner:
    """Plans chunked prefill for a set of requests under a per-step token budget."""

    def __init__(self, chunk_size: int = 512, *, step_token_budget: int = 2048) -> None:
        """Hold the maximum chunk size and the per-step prefill token budget."""
        if chunk_size < 1 or step_token_budget < 1:
            raise ValueError("chunk_size and step_token_budget must be >= 1")
        self._chunk_size = chunk_size
        self._budget = step_token_budget

    def chunks_for(self, request: Request) -> list[PrefillChunk]:
        """The ordered prefill chunks for a single request."""
        chunks: list[PrefillChunk] = []
        n = request.prompt_len
        start = 0
        while start < n:
            end = min(n, start + self._chunk_size)
            chunks.append(PrefillChunk(request.request_id, start, end, is_last=end >= n))
            start = end
        if not chunks:
            chunks.append(PrefillChunk(request.request_id, 0, 0, is_last=True))
        return chunks

    def plan_step(self, pending: list[PrefillChunk]) -> tuple[list[PrefillChunk], list[PrefillChunk]]:
        """Greedily pack pending chunks into one step under the token budget; return (step, remaining)."""
        this_step: list[PrefillChunk] = []
        used = 0
        for i, chunk in enumerate(pending):
            if this_step and used + chunk.length > self._budget:
                return this_step, pending[i:]
            this_step.append(chunk)
            used += chunk.length
        return this_step, []
