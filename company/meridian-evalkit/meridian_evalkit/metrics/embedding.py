"""The reference-embedding metric: mean cosine similarity of predictions to their references.

Each token sequence is embedded into a fixed-width, deterministic vector, and the metric reports the mean
cosine similarity between every record's prediction embedding and its reference embedding. The reference
embeddings are computed from the records on each call. The embedding function is stable across releases so the
metric is reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt

from meridian_common import cost
from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics.records import ScoreRecord

EMBED_DIM = 16


def embed(tokens: tuple[int, ...], *, dim: int = EMBED_DIM) -> tuple[float, ...]:
    """A deterministic, L2-normalized embedding of ``tokens`` into ``dim`` components.

    Each token contributes a decaying weight to the component selected by a stable hash of its position and
    value, giving distinct sequences distinct directions. The empty sequence embeds to the zero vector.
    """
    if dim < 1:
        raise MetricError("embedding dim must be >= 1", code="evalkit.metric", dim=dim)
    vec = [0.0] * dim
    for i, tok in enumerate(tokens):
        h = (2654435761 * (tok + 1) + 40503 * (i + 1)) & 0xFFFFFFFF
        sign = 1.0 if (h >> 16) & 1 else -1.0
        weight = sign / (1.0 + i)
        vec[h % dim] += weight
    norm = sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return tuple(vec)
    return tuple(v / norm for v in vec)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """The cosine similarity of two equal-length, already-normalized vectors (0.0 if either is zero)."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def reference_embedding_similarity(records: Sequence[ScoreRecord], *, dim: int = EMBED_DIM) -> float:
    """The mean cosine similarity between each record's prediction and reference embeddings.

    Both embeddings are computed on this call. A record with an empty reference contributes a similarity of
    ``0.0`` (its reference direction is undefined).
    """
    if not records:
        raise MetricError("reference-embedding metric needs at least one record", code="evalkit.metric")
    total = 0.0
    for r in records:
        ref_vec = embed(r.reference, dim=dim)
        pred_vec = embed(r.prediction, dim=dim)
        cost.record("evalkit.embed_recompute", 1)  # a reference-embedding recompute for this record
        total += _cosine(pred_vec, ref_vec)
    return total / len(records)
