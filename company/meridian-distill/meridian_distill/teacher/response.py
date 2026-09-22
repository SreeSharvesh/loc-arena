"""The teacher response value type and its feature computation.

A :class:`TeacherResponse` is one teacher query outcome: the request key it was cached under, the prompt it
answered, the generated token ids, and a fixed-width feature vector derived from those tokens. The feature
computation is a stable public surface (:func:`teacher_features`) that downstream consumers (evalkit) reuse,
so its shape and values must stay deterministic across releases.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_FEATURE_DIM = 8


def compute_features(tokens: tuple[int, ...], *, dim: int = DEFAULT_FEATURE_DIM) -> tuple[float, ...]:
    """A deterministic, length-normalized feature vector over ``tokens`` with ``dim`` components.

    The first ``dim - 1`` components are the fraction of tokens falling in each residue bucket
    (``token % (dim - 1)``); the final component is the mean token value normalized to ``[0, 1)``. An empty
    token sequence yields the zero vector. Rounding keeps the values stable under re-computation.
    """
    if dim < 2:
        raise ValueError("feature dim must be at least 2")
    if not tokens:
        return tuple(0.0 for _ in range(dim))
    buckets = dim - 1
    hist = [0] * buckets
    for tok in tokens:
        hist[tok % buckets] += 1
    n = len(tokens)
    mean = sum(tokens) / n
    vec = [round(count / n, 12) for count in hist]
    vec.append(round((mean % 1000) / 1000.0, 12))
    return tuple(vec)


@dataclass(frozen=True)
class TeacherResponse:
    """A teacher query outcome: its cache key, the prompt, the generated tokens, and the feature vector."""

    request_key: str
    prompt: tuple[int, ...]
    tokens: tuple[int, ...]
    features: tuple[float, ...]

    @property
    def generated_len(self) -> int:
        """The number of generated tokens."""
        return len(self.tokens)


def teacher_features(response: TeacherResponse) -> tuple[float, ...]:
    """The feature vector for ``response`` (the stable surface downstream consumers reuse)."""
    return response.features
