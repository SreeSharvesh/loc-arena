"""Admission control: decide whether to accept a request before it enters the queue.

The controller enforces per-request limits (a maximum prompt length and a maximum total sequence length) and a
global in-flight token budget, so a burst of long requests cannot exhaust the KV cache. It reports a typed
:class:`AdmissionDecision` and tracks the current in-flight token count as requests are admitted and retired.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_serving.types import Request


@dataclass(frozen=True)
class AdmissionDecision:
    """Whether a request is admitted, and if not, a stable reason code."""

    admitted: bool
    reason: str = "ok"


@dataclass(frozen=True)
class AdmissionLimits:
    """The configured admission limits."""

    max_prompt_len: int = 4096
    max_total_len: int = 8192
    max_inflight_tokens: int = 262144


class AdmissionController:
    """Applies :class:`AdmissionLimits` and tracks in-flight tokens."""

    def __init__(self, limits: AdmissionLimits | None = None) -> None:
        """Hold the limits and start with zero in-flight tokens."""
        self._limits = limits if limits is not None else AdmissionLimits()
        self._inflight = 0

    @property
    def inflight_tokens(self) -> int:
        """The current number of admitted-but-not-retired tokens."""
        return self._inflight

    def evaluate(self, request: Request) -> AdmissionDecision:
        """Decide whether ``request`` may be admitted under the current limits (does not mutate state)."""
        if request.prompt_len > self._limits.max_prompt_len:
            return AdmissionDecision(False, "prompt_too_long")
        if request.total_len > self._limits.max_total_len:
            return AdmissionDecision(False, "sequence_too_long")
        if self._inflight + request.total_len > self._limits.max_inflight_tokens:
            return AdmissionDecision(False, "inflight_budget_exceeded")
        return AdmissionDecision(True)

    def admit(self, request: Request) -> AdmissionDecision:
        """Evaluate ``request`` and, if admitted, reserve its tokens against the in-flight budget."""
        decision = self.evaluate(request)
        if decision.admitted:
            self._inflight += request.total_len
        return decision

    def retire(self, request: Request) -> None:
        """Release ``request``'s reserved tokens back to the in-flight budget."""
        self._inflight = max(0, self._inflight - request.total_len)
