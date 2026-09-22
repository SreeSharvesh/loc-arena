"""Speculative decoding: a cheap draft proposes tokens a target model verifies in one pass.

A small draft model proposes ``k`` tokens; the target model verifies them in a single pass and accepts the
longest matching prefix, then emits one correction token. On average this produces more than one token per
target step, trading extra draft compute for fewer target passes. The scheduler here is the deterministic
accept/reject bookkeeping over a draft and a target logit function; the models are injected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from meridian_serving.sampler.sampling import Sampler

LogitFn = Callable[[tuple[int, ...]], list[float]]


@dataclass(frozen=True)
class SpeculationResult:
    """One speculative step: the accepted tokens, how many were proposed, and target passes used."""

    accepted: tuple[int, ...]
    proposed: int
    target_passes: int

    @property
    def accepted_len(self) -> int:
        """How many tokens were accepted this step."""
        return len(self.accepted)


class SpeculativeDecoder:
    """Runs draft-and-verify speculative decoding with a fixed lookahead."""

    def __init__(
        self,
        draft_logits: LogitFn,
        target_logits: LogitFn,
        *,
        lookahead: int = 4,
        sampler: Sampler | None = None,
    ) -> None:
        """Wire the draft and target logit functions, the lookahead, and a greedy verification sampler."""
        if lookahead < 1:
            raise ValueError("lookahead must be >= 1")
        self._draft = draft_logits
        self._target = target_logits
        self._lookahead = lookahead
        self._sampler = sampler if sampler is not None else Sampler(temperature=1.0, top_p=1.0)

    def _propose(self, context: tuple[int, ...]) -> list[int]:
        proposed: list[int] = []
        cursor = context
        for _ in range(self._lookahead):
            token = self._sampler.argmax(self._draft(cursor))
            proposed.append(token)
            cursor = (*cursor, token)
        return proposed

    def step(self, context: tuple[int, ...]) -> SpeculationResult:
        """Propose ``lookahead`` tokens and accept the prefix the target agrees with, plus one correction."""
        proposed = self._propose(context)
        accepted: list[int] = []
        cursor = context
        for token in proposed:
            target_token = self._sampler.argmax(self._target(cursor))
            if target_token == token:
                accepted.append(token)
                cursor = (*cursor, token)
            else:
                accepted.append(target_token)  # the correction token from the target
                cursor = (*cursor, target_token)
                break
        else:
            # all proposals accepted: the target emits one more token
            accepted.append(self._sampler.argmax(self._target(cursor)))
        return SpeculationResult(accepted=tuple(accepted), proposed=len(proposed), target_passes=1)

    def decode(self, prompt: tuple[int, ...], *, max_tokens: int) -> tuple[int, ...]:
        """Decode up to ``max_tokens`` tokens from ``prompt`` using speculative steps."""
        context = prompt
        out: list[int] = []
        while len(out) < max_tokens:
            result = self.step(context)
            for token in result.accepted:
                if len(out) >= max_tokens:
                    break
                out.append(token)
                context = (*context, token)
        return tuple(out)
