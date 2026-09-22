"""Leaderboard: rank runs by score and measure the benchmark's discriminative power.

Owns the :class:`Leaderboard` and its :class:`LeaderboardEntry` rows, plus the discriminative-power and
score-spread analyses over the ranked entries.
"""

from __future__ import annotations

from meridian_evalkit.leaderboard.board import Leaderboard, LeaderboardEntry
from meridian_evalkit.leaderboard.power import discriminative_power, score_spread

__all__ = [
    "Leaderboard",
    "LeaderboardEntry",
    "discriminative_power",
    "score_spread",
]
