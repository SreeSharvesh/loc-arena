"""Typical sampling and mirostat, two alternatives to top-p that target an information content.

Typical sampling keeps tokens whose surprisal (``-log p``) is closest to the distribution's entropy, so it
prunes both the boringly-likely and the wildly-unlikely tail. Mirostat adjusts an effective top-k each
step via
a feedback loop to hold the output's surprisal near a target, stabilizing perceived quality. Both are
deterministic given the logits (and, for mirostat, its running state).
"""

from __future__ import annotations

import math

from meridian_serving.errors import SamplerError
from meridian_serving.sampler.sampling import softmax


def entropy(probs: list[float]) -> float:
    """The Shannon entropy (in nats) of a probability vector."""
    return -sum(p * math.log(p) for p in probs if p > 0.0)


def typical_mask(logits: list[float], *, mass: float) -> set[int]:
    """The set of tokens whose surprisal is closest to the entropy, accumulating up to ``mass``."""
    if not 0.0 < mass <= 1.0:
        raise SamplerError("mass must be in (0, 1]")
    probs = softmax(logits)
    ent = entropy(probs)
    # rank tokens by how close their surprisal is to the entropy (the "typical" set)
    order = sorted(range(len(probs)), key=lambda i: abs(-math.log(probs[i] + 1e-12) - ent))
    kept: set[int] = set()
    cumulative = 0.0
    for i in order:
        kept.add(i)
        cumulative += probs[i]
        if cumulative >= mass:
            break
    return kept


class Mirostat:
    """Mirostat v1-style feedback control of an effective top-k to hold output surprisal near a target."""

    def __init__(self, *, target_surprisal: float = 3.0, learning_rate: float = 0.1) -> None:
        """Hold the target surprisal (nats) and learning rate; start ``mu`` at twice the target."""
        if target_surprisal <= 0:
            raise SamplerError("target_surprisal must be positive")
        self._target = target_surprisal
        self._lr = learning_rate
        self._mu = 2.0 * target_surprisal

    @property
    def mu(self) -> float:
        """The current control variable (an upper bound on acceptable surprisal)."""
        return self._mu

    def select(self, logits: list[float]) -> int:
        """Pick the most likely token whose surprisal is under ``mu`` and update ``mu`` from the error."""
        probs = softmax(logits)
        order = sorted(range(len(probs)), key=lambda i: (probs[i], -i), reverse=True)
        chosen = order[0]
        for i in order:
            if -math.log(probs[i] + 1e-12) <= self._mu:
                chosen = i
                break
        observed = -math.log(probs[chosen] + 1e-12)
        self._mu = max(0.0, self._mu - self._lr * (observed - self._target))
        return chosen

    def reset(self) -> None:
        """Reset the control variable to its initial value."""
        self._mu = 2.0 * self._target
