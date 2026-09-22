"""Composable logit processors applied before sampling.

A :class:`LogitProcessor` transforms a logit vector given the generation context (the tokens already emitted).
They chain in a :class:`ProcessorPipeline`, so a request can stack a logit bias, presence and frequency
penalties, a min-p floor, and a bad-words ban in a fixed, deterministic order. Each processor is pure: it
returns a new vector and never mutates its input.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Protocol

from meridian_serving.errors import SamplerError

_NEG_INF = float("-inf")


class LogitProcessor(Protocol):
    """Transforms a logit vector given the already-emitted tokens."""

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Return a new logit vector."""
        ...


class LogitBias:
    """Adds a fixed per-token bias to the logits (positive promotes, negative suppresses)."""

    def __init__(self, bias: dict[int, float]) -> None:
        """Hold the per-token bias map."""
        self._bias = dict(bias)

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Add the configured bias to each biased token's logit."""
        out = list(logits)
        for token, delta in self._bias.items():
            if 0 <= token < len(out):
                out[token] += delta
        return out


class BadWordsProcessor:
    """Bans a set of tokens by driving their logits to negative infinity."""

    def __init__(self, banned: set[int]) -> None:
        """Hold the banned token set."""
        self._banned = set(banned)

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Set every banned token's logit to negative infinity."""
        out = list(logits)
        for token in self._banned:
            if 0 <= token < len(out):
                out[token] = _NEG_INF
        return out


class PresencePenalty:
    """Subtracts a flat penalty from any token that has appeared at least once (presence penalty)."""

    def __init__(self, penalty: float) -> None:
        """Hold the presence penalty."""
        self._penalty = penalty

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Subtract the penalty from every token already present in ``previous``."""
        out = list(logits)
        for token in set(previous):
            if 0 <= token < len(out):
                out[token] -= self._penalty
        return out


class FrequencyPenalty:
    """Subtracts a penalty proportional to how often a token has already appeared (frequency penalty)."""

    def __init__(self, penalty: float) -> None:
        """Hold the frequency penalty."""
        self._penalty = penalty

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Subtract ``penalty * count`` from each token by its occurrence count in ``previous``."""
        out = list(logits)
        counts = Counter(previous)
        for token, count in counts.items():
            if 0 <= token < len(out):
                out[token] -= self._penalty * count
        return out


class MinPProcessor:
    """Bans tokens whose probability is below ``min_p`` times the peak probability (min-p filtering)."""

    def __init__(self, min_p: float) -> None:
        """Hold the min-p fraction in [0, 1]."""
        if not 0.0 <= min_p <= 1.0:
            raise SamplerError("min_p must be in [0, 1]")
        self._min_p = min_p

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Ban tokens whose softmax probability is under ``min_p`` of the maximum probability."""
        if self._min_p <= 0.0:
            return list(logits)
        finite = [x for x in logits if x != _NEG_INF]
        if not finite:
            return list(logits)
        hi = max(finite)
        exps = [math.exp(x - hi) if x != _NEG_INF else 0.0 for x in logits]
        peak = max(exps)
        threshold = self._min_p * peak
        return [x if e >= threshold else _NEG_INF for x, e in zip(logits, exps, strict=True)]


class ProcessorPipeline:
    """Applies a fixed sequence of :class:`LogitProcessor` objects in order."""

    def __init__(self, processors: list[LogitProcessor] | None = None) -> None:
        """Hold the ordered processors."""
        self._processors = list(processors or [])

    def add(self, processor: LogitProcessor) -> ProcessorPipeline:
        """Append a processor and return ``self`` for chaining."""
        self._processors.append(processor)
        return self

    def process(self, logits: list[float], previous: list[int]) -> list[float]:
        """Run every processor in order over ``logits``."""
        out = list(logits)
        for proc in self._processors:
            out = proc.process(out, previous)
        return out

    def __len__(self) -> int:
        """The number of processors in the pipeline."""
        return len(self._processors)
