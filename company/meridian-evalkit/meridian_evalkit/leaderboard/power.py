"""The discriminative-power analysis: how well a benchmark separates models.

A benchmark that scores every model the same tells nothing apart; one that spreads scores out ranks models
confidently. :func:`discriminative_power` reports the fraction of distinct model pairs whose scores differ by
more than a margin (``1.0`` means every pair is separated, ``0.0`` means all ties). :func:`score_spread` is
the raw range of scores. Both are deterministic pure functions over leaderboard entries.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations

from meridian_evalkit.errors import LeaderboardError
from meridian_evalkit.leaderboard.board import LeaderboardEntry


def score_spread(entries: Sequence[LeaderboardEntry]) -> float:
    """The range (max minus min) of the entries' scores (raises on an empty input)."""
    if not entries:
        raise LeaderboardError("score spread needs at least one entry", code="evalkit.leaderboard")
    scores = [e.score for e in entries]
    return max(scores) - min(scores)


def discriminative_power(entries: Sequence[LeaderboardEntry], *, margin: float = 0.0) -> float:
    """The fraction of distinct entry pairs whose scores differ by more than ``margin``.

    With a single entry there are no pairs to separate, so the power is ``0.0``. A negative margin is
    rejected. The result lies in ``[0, 1]``; higher means the benchmark separates runs more sharply.
    """
    if margin < 0.0:
        raise LeaderboardError("margin must be non-negative", code="evalkit.leaderboard", margin=margin)
    if len(entries) < 2:
        return 0.0
    pairs = list(combinations(entries, 2))
    separated = sum(1 for a, b in pairs if abs(a.score - b.score) > margin)
    return separated / len(pairs)
