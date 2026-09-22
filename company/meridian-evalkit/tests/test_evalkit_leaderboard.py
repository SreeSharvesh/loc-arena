"""Tests for the leaderboard and the discriminative-power analysis."""

from __future__ import annotations

import pytest

from meridian_evalkit.errors import LeaderboardError
from meridian_evalkit.leaderboard import (
    Leaderboard,
    LeaderboardEntry,
    discriminative_power,
    score_spread,
)
from meridian_evalkit.store import EvalRun


def _board(*scores: float) -> Leaderboard:
    board = Leaderboard()
    for i, s in enumerate(scores):
        board.add(LeaderboardEntry(run_id=f"r{i}", model=f"m{i}", score=s))
    return board


def test_ranking_distinct_scores_is_descending() -> None:
    board = _board(0.1, 0.9, 0.5)
    assert [e.run_id for e in board.ranking()] == ["r1", "r2", "r0"]


def test_ranking_distinct_scores_is_stable() -> None:
    board = _board(0.1, 0.9, 0.5)
    orders = {tuple(e.run_id for e in board.ranking()) for _ in range(50)}
    assert len(orders) == 1


def test_top_n() -> None:
    board = _board(0.1, 0.9, 0.5)
    top = board.top(2)
    assert [e.run_id for e in top] == ["r1", "r2"]


def test_rank_of() -> None:
    board = _board(0.1, 0.9, 0.5)
    assert board.rank_of("r1") == 1
    assert board.rank_of("r2") == 2
    assert board.rank_of("r0") == 3


def test_rank_of_unknown_raises() -> None:
    with pytest.raises(LeaderboardError):
        _board(0.5).rank_of("nope")


def test_duplicate_run_id_raises() -> None:
    board = Leaderboard()
    board.add(LeaderboardEntry("r0", "m", 0.5))
    with pytest.raises(LeaderboardError):
        board.add(LeaderboardEntry("r0", "m", 0.6))


def test_empty_ranking_raises() -> None:
    with pytest.raises(LeaderboardError):
        Leaderboard().ranking()


def test_add_run_from_eval_run() -> None:
    board = Leaderboard()
    board.add_run(EvalRun(run_id="r1", model="det", metrics={"accuracy": 0.8}), metric="accuracy")
    assert board.ranking()[0].score == 0.8
    assert board.ranking()[0].model == "det"


def test_add_run_missing_metric_raises() -> None:
    board = Leaderboard()
    with pytest.raises(LeaderboardError):
        board.add_run(EvalRun(run_id="r1", model="det", metrics={"ece": 0.1}), metric="accuracy")


def test_discriminative_power_all_distinct() -> None:
    entries = _board(0.1, 0.5, 0.9).entries
    assert discriminative_power(entries) == pytest.approx(1.0)


def test_discriminative_power_with_a_tie() -> None:
    entries = _board(0.9, 0.5, 0.5).entries
    assert discriminative_power(entries) == pytest.approx(2 / 3)


def test_discriminative_power_single_entry_is_zero() -> None:
    assert discriminative_power(_board(0.5).entries) == 0.0


def test_discriminative_power_negative_margin_raises() -> None:
    with pytest.raises(LeaderboardError):
        discriminative_power(_board(0.1, 0.2).entries, margin=-0.1)


def test_score_spread() -> None:
    assert score_spread(_board(0.9, 0.5, 0.1).entries) == pytest.approx(0.8)


def test_score_spread_empty_raises() -> None:
    with pytest.raises(LeaderboardError):
        score_spread([])
