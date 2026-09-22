from __future__ import annotations

from pathlib import Path

from loc_arena.metrics import calibrate_threshold

from tests.integration._p5_support import honest_record, protocol


def test_threshold_from_honest_distribution_and_moves_with_budget(tmp_path: Path) -> None:
    proto = protocol()
    honest = [honest_record(tmp_path / f"h{i}", proto) for i in range(2)]
    tight = calibrate_threshold(honest, 0.02)  # a small audit budget -> a high threshold
    loose = calibrate_threshold(honest, 0.5)  # a large budget -> a lower threshold
    assert tight >= loose  # a smaller FPR yields at least as high a threshold
    assert tight != loose  # the honest distribution has spread, so the budget changes the threshold
    # the threshold sits within the observed honest suspicion range
    honest_scores = [v.score for r in honest for v in r.verdicts]
    assert min(honest_scores) <= tight <= max(honest_scores)


def test_empty_honest_distribution_falls_back(tmp_path: Path) -> None:
    assert calibrate_threshold([], 0.02) == 0.5
