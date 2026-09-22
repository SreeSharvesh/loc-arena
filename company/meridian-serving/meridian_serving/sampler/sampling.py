"""Deterministic token sampling: temperature, top-k, top-p (nucleus), and a repetition penalty.

Given a logit vector and a seed the sampler is fully deterministic. The pipeline applies a repetition penalty
to already-emitted tokens, scales logits by temperature, forms a probability distribution, restricts it to the
top-k most likely tokens and to the smallest nucleus whose mass reaches ``top_p``, renormalizes, and draws one
token. The building blocks are exposed (``softmax``, ``nucleus``) so callers can inspect the candidate set.
"""

from __future__ import annotations

import math
import random

from meridian_serving.errors import SamplerError


def softmax(logits: list[float]) -> list[float]:
    """A numerically stable softmax over ``logits``."""
    if not logits:
        raise SamplerError("softmax of an empty logit vector")
    hi = max(logits)
    exps = [math.exp(x - hi) for x in logits]
    total = sum(exps)
    return [e / total for e in exps]


def _apply_repetition_penalty(logits: list[float], previous: list[int], penalty: float) -> list[float]:
    """Divide the logit of each previously-emitted token by ``penalty`` (or multiply if negative)."""
    if penalty == 1.0 or not previous:
        return list(logits)
    out = list(logits)
    seen = set(previous)
    for tok in seen:
        if 0 <= tok < len(out):
            out[tok] = out[tok] / penalty if out[tok] > 0 else out[tok] * penalty
    return out


def _top_k_mask(probs: list[float], k: int) -> set[int]:
    """The indices of the ``k`` highest-probability tokens (all of them if ``k <= 0`` or ``k >= len``)."""
    if k <= 0 or k >= len(probs):
        return set(range(len(probs)))
    order = sorted(range(len(probs)), key=lambda i: (probs[i], -i), reverse=True)
    return set(order[:k])


def _nucleus_mask(probs: list[float], top_p: float) -> set[int]:
    """The smallest set of highest-probability tokens whose cumulative mass reaches ``top_p``."""
    if top_p >= 1.0:
        return set(range(len(probs)))
    order = sorted(range(len(probs)), key=lambda i: (probs[i], -i), reverse=True)
    kept: set[int] = set()
    cumulative = 0.0
    for i in order:
        kept.add(i)
        cumulative += probs[i]
        if cumulative >= top_p:
            break
    return kept


class Sampler:
    """A deterministic token sampler configured with temperature, top-k, top-p, and a repetition penalty."""

    def __init__(
        self,
        *,
        temperature: float = 1.0,
        top_k: int = 0,
        top_p: float = 1.0,
        repetition_penalty: float = 1.0,
    ) -> None:
        """Hold and validate the sampling parameters."""
        if temperature <= 0:
            raise SamplerError("temperature must be positive")
        if not 0.0 < top_p <= 1.0:
            raise SamplerError("top_p must be in (0, 1]")
        if repetition_penalty <= 0:
            raise SamplerError("repetition_penalty must be positive")
        self._temperature = temperature
        self._top_k = top_k
        self._top_p = top_p
        self._rep = repetition_penalty

    def _scaled_probs(self, logits: list[float], previous: list[int]) -> list[float]:
        penalized = _apply_repetition_penalty(logits, previous, self._rep)
        scaled = [x / self._temperature for x in penalized]
        return softmax(scaled)

    def nucleus(self, logits: list[float], previous: list[int] | None = None) -> set[int]:
        """The candidate token set this sampler would sample from for ``logits``."""
        previous = previous or []
        probs = self._scaled_probs(logits, previous)
        base = softmax(_apply_repetition_penalty(logits, previous, self._rep))
        keep = _top_k_mask(probs, self._top_k) & _nucleus_mask(base, self._top_p)
        return keep or _top_k_mask(probs, self._top_k)

    def sample(self, logits: list[float], *, seed: int, previous: list[int] | None = None) -> int:
        """Draw one token id from ``logits`` deterministically given ``seed``."""
        if not logits:
            raise SamplerError("cannot sample from an empty logit vector")
        previous = previous or []
        probs = self._scaled_probs(logits, previous)
        keep = self.nucleus(logits, previous)
        masked = [(i, probs[i]) for i in sorted(keep)]
        total = sum(p for _, p in masked)
        if total <= 0.0:
            raise SamplerError("no probability mass in the candidate set")
        rng = random.Random(seed)
        draw = rng.random() * total
        cumulative = 0.0
        for idx, p in masked:
            cumulative += p
            if draw <= cumulative:
                return idx
        return masked[-1][0]

    def argmax(self, logits: list[float], previous: list[int] | None = None) -> int:
        """The greedy token: the highest-probability token within the candidate set."""
        previous = previous or []
        probs = self._scaled_probs(logits, previous)
        keep = self.nucleus(logits, previous)
        return max(sorted(keep), key=lambda i: probs[i])
