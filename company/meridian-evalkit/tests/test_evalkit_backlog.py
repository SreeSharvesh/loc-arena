"""Contract tests tracking open backlog tickets against meridian-evalkit.

Each is a terse ``strict`` xfail tied to its ticket: it asserts the documented contract, currently fails, and
turns into a hard failure the moment the ticket is resolved (prompting the marker's removal). Mechanisms are
intentionally not described here.
"""

from __future__ import annotations

import pytest

from meridian_evalkit.leaderboard import Leaderboard, LeaderboardEntry
from meridian_evalkit.metrics import RunningAccuracy, ScoreRecord


@pytest.mark.xfail(strict=True, reason="MER-EVALKIT-41 (open)")
def test_rescored_item_counts_once() -> None:
    acc = RunningAccuracy()
    acc.observe(ScoreRecord(item_id="a", correct=False))
    acc.observe(ScoreRecord(item_id="a", correct=True))
    acc.observe(ScoreRecord(item_id="b", correct=True))
    acc.observe(ScoreRecord(item_id="c", correct=False))
    assert acc.value == pytest.approx(2 / 3)


@pytest.mark.xfail(strict=True, reason="MER-EVALKIT-58 (open)")
def test_tied_ranking_is_stable() -> None:
    board = Leaderboard()
    board.add(LeaderboardEntry(run_id="r1", model="m1", score=0.5))
    board.add(LeaderboardEntry(run_id="r2", model="m2", score=0.5))
    board.add(LeaderboardEntry(run_id="r3", model="m3", score=0.5))
    orders = {tuple(e.run_id for e in board.ranking()) for _ in range(200)}
    assert len(orders) == 1
