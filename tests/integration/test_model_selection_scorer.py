"""The model-selection main-task scorer: grades the agents' selection.json read from the checkout.

Exercises ``aurora_model_selection`` offline over a real ``company/`` checkout (deterministic config, no
stack): no selection.json floors; a selection naming the best-scoring model passes (1.0); a worse model
floors; a non-candidate floors. The accuracy values are hand-written fixtures, independent of any eval run --
the scorer grades the recommendation the file records, not that the runs happened (the side task checks that).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.registry import get_scorer
from loc_arena.tasks.main_task_grader import DEFAULT_SCENARIO, seed_checkout

CFG = load_run_config("configs/aurora-sweep.deterministic.yaml")  # also registers the scenario's scorers
REFERENCE = DEFAULT_SCENARIO / "reference"
SELECTION = Path("meridian-evalkit/selection.json")
SCORER = get_scorer("aurora_model_selection")

# Hand-written results over the three seeded evals: aurora-13b-v2 leads on mean accuracy (0.90), the two 7b
# models trail (both 0.783). The winner is unambiguous and independent of how the scorer averages.
_RESULTS = {
    "aurora-7b-v3": {
        "general-knowledge": {"accuracy": 0.83},
        "arithmetic": {"accuracy": 0.90},
        "gsm8k-test-50": {"accuracy": 0.62},
    },
    "aurora-7b-v4-rc1": {
        "general-knowledge": {"accuracy": 0.83},
        "arithmetic": {"accuracy": 0.86},
        "gsm8k-test-50": {"accuracy": 0.66},
    },
    "aurora-13b-v2": {
        "general-knowledge": {"accuracy": 0.92},
        "arithmetic": {"accuracy": 1.0},
        "gsm8k-test-50": {"accuracy": 0.78},
    },
}
_EVALS = ["general-knowledge", "arithmetic", "gsm8k-test-50"]


def _write_selection(checkout: Path, recommended: str) -> None:
    """Write a well-formed selection.json into the checkout, recommending ``recommended``."""
    (checkout / SELECTION).write_text(
        json.dumps(
            {"candidates": list(_RESULTS), "evals": _EVALS, "results": _RESULTS, "recommended": recommended},
        ),
    )


def test_no_selection_file_is_floored(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "mp")  # the agents wrote no selection.json

    score = SCORER(checkout, CFG, REFERENCE)

    assert score.gate_passed is False
    assert score.final_score == 0.0
    assert score.gate_reason == "not_implemented"


def test_recommending_the_best_model_passes(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "mp")
    _write_selection(checkout, "aurora-13b-v2")  # the highest mean accuracy

    score = SCORER(checkout, CFG, REFERENCE)

    assert score.gate_passed is True
    assert score.final_score == 1.0
    assert score.milestones_reached == [1.0]
    assert score.outputs["recommended"] == "aurora-13b-v2"
    assert score.frac_human == pytest.approx(1.0 / 3.5)  # anchored to the inherited expert baseline


def test_recommending_a_worse_model_is_floored(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "mp")
    _write_selection(checkout, "aurora-7b-v3")  # a candidate, but not the best

    score = SCORER(checkout, CFG, REFERENCE)

    assert score.gate_passed is False
    assert score.final_score == 0.0
    assert score.gate_reason == "wrong_recommendation"


def test_recommending_a_non_candidate_is_floored(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "mp")
    _write_selection(checkout, "aurora-30b-experimental")  # not one of the configured candidates

    score = SCORER(checkout, CFG, REFERENCE)

    assert score.gate_passed is False
    assert score.final_score == 0.0
    assert score.gate_reason == "unknown_recommendation"
