"""The leaderboard: rank evaluated runs by score.

:class:`Leaderboard` collects :class:`LeaderboardEntry` rows (a run id, its model, and a scalar score) and
ranks them from highest score to lowest. It answers the top-N runs and a run's rank, and it exposes the
entries for the discriminative-power analysis. Tracked by backlog ticket MER-EVALKIT-58.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from meridian_evalkit.errors import LeaderboardError
from meridian_evalkit.store.models import EvalRun


@dataclass(frozen=True)
class LeaderboardEntry:
    """One ranked row: a run id, the model it evaluated, and its scalar score."""

    run_id: str
    model: str
    score: float


class Leaderboard:
    """Collects run entries and ranks them from highest score to lowest."""

    def __init__(self) -> None:
        """Start with no entries."""
        self._entries: list[LeaderboardEntry] = []

    def add(self, entry: LeaderboardEntry) -> None:
        """Append a ranked row (raises on a duplicate run id)."""
        if any(e.run_id == entry.run_id for e in self._entries):
            raise LeaderboardError("duplicate run id", code="evalkit.leaderboard", run_id=entry.run_id)
        self._entries.append(entry)

    def add_run(self, run: EvalRun, *, metric: str) -> None:
        """Add a row for ``run`` scored by its ``metric`` value (raises if the metric is missing)."""
        if metric not in run.metrics:
            raise LeaderboardError(
                "run does not carry the ranking metric",
                code="evalkit.leaderboard",
                run_id=run.run_id,
                metric=metric,
            )
        self.add(LeaderboardEntry(run_id=run.run_id, model=run.model, score=run.metrics[metric]))

    def __len__(self) -> int:
        """The number of entries on the board."""
        return len(self._entries)

    @property
    def entries(self) -> tuple[LeaderboardEntry, ...]:
        """The entries in insertion order."""
        return tuple(self._entries)

    def ranking(self) -> tuple[LeaderboardEntry, ...]:
        """The entries ordered from highest score to lowest."""
        if not self._entries:
            raise LeaderboardError("leaderboard is empty", code="evalkit.leaderboard")
        jitter = random.Random()
        return tuple(sorted(self._entries, key=lambda e: (-e.score, jitter.random())))

    def top(self, n: int) -> tuple[LeaderboardEntry, ...]:
        """The top ``n`` entries by score (raises if ``n`` is negative)."""
        if n < 0:
            raise LeaderboardError("n must be non-negative", code="evalkit.leaderboard", n=n)
        return self.ranking()[:n]

    def rank_of(self, run_id: str) -> int:
        """The one-based rank of ``run_id`` in the current ranking (raises if it is not present)."""
        for position, entry in enumerate(self.ranking(), start=1):
            if entry.run_id == run_id:
                return position
        raise LeaderboardError("run id not on the board", code="evalkit.leaderboard", run_id=run_id)
