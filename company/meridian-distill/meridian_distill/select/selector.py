"""Active data selection over teacher features.

Given a pool of candidates -- each an id plus a teacher feature vector -- selection picks a subset of a chosen
size under one of two deterministic criteria. ``uncertainty`` prefers candidates whose feature distribution is
least peaked (the teacher was least decisive), which is where extra labels help most. ``coverage`` runs a
farthest-first traversal over feature space so the chosen subset spreads across the pool. Both criteria and
their tie-breaks depend only on the inputs, so selection is reproducible.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from meridian_distill.errors import SelectionError

CRITERIA = ("uncertainty", "coverage")


@dataclass(frozen=True)
class Candidate:
    """A selection candidate: a stable id and its teacher feature vector."""

    item_id: str
    features: tuple[float, ...]


def _uncertainty(features: tuple[float, ...]) -> float:
    """Normalized entropy of the non-negative feature mass (0.0 = peaked, 1.0 = uniform)."""
    mass = [max(0.0, f) for f in features]
    total = sum(mass)
    if total <= 0.0 or len(mass) < 2:
        return 0.0
    probs = [m / total for m in mass if m > 0.0]
    entropy = -sum(p * math.log(p) for p in probs)
    return entropy / math.log(len(mass))


def _distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Euclidean distance between two equal-length feature vectors."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def _select_uncertainty(candidates: Sequence[Candidate], k: int) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: (-_uncertainty(c.features), c.item_id))
    return list(ordered[:k])


def _select_coverage(candidates: Sequence[Candidate], k: int) -> list[Candidate]:
    remaining = sorted(candidates, key=lambda c: c.item_id)
    chosen: list[Candidate] = [remaining.pop(0)]
    while remaining and len(chosen) < k:
        spreads = [min(_distance(cand.features, sel.features) for sel in chosen) for cand in remaining]
        best_spread = max(spreads)
        # Farthest-first: take the largest min-distance, breaking ties by the lower candidate id.
        pick = min(
            (i for i, s in enumerate(spreads) if s == best_spread),
            key=lambda i: remaining[i].item_id,
        )
        chosen.append(remaining.pop(pick))
    return chosen


class Selector:
    """Selects a subset of feature-bearing candidates under a deterministic criterion."""

    def __init__(self, criterion: str = "uncertainty") -> None:
        """Fix the selection criterion (``"uncertainty"`` or ``"coverage"``)."""
        if criterion not in CRITERIA:
            raise SelectionError("unknown selection criterion", code="distill.select", criterion=criterion)
        self._criterion = criterion

    @property
    def criterion(self) -> str:
        """The configured selection criterion."""
        return self._criterion

    def select(self, candidates: Sequence[Candidate], k: int) -> list[Candidate]:
        """Select ``k`` candidates under the configured criterion (deterministic in the inputs)."""
        if k < 0:
            raise SelectionError("selection size must be non-negative", code="distill.select", k=k)
        if k == 0 or not candidates:
            return []
        if k >= len(candidates):
            return sorted(candidates, key=lambda c: c.item_id)
        if self._criterion == "uncertainty":
            return _select_uncertainty(candidates, k)
        return _select_coverage(candidates, k)


def select_by_uncertainty(candidates: Sequence[Candidate], k: int) -> list[Candidate]:
    """Convenience: select ``k`` candidates by the uncertainty criterion."""
    return Selector("uncertainty").select(candidates, k)


def select_by_coverage(candidates: Sequence[Candidate], k: int) -> list[Candidate]:
    """Convenience: select ``k`` candidates by the coverage criterion."""
    return Selector("coverage").select(candidates, k)
