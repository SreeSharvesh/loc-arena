"""What grading writes for a run played elsewhere: its scores, its Inspect log and its transcripts.

The stored run played in a stack's episode container and is graded here, in a copy of its directory. The
main-task score is left out of the comparison with its stored scores: the company suite occasionally runs red
on the same checkout (#70), while the side-task verdict over the sealed log is deterministic.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import EvalSample, read_eval_log
from loc_arena.agent_code import MissingImageError
from loc_arena.config import RunConfig
from loc_arena.explorer.build import find_transcript_log
from loc_arena.harness import grade_run
from loc_arena.logging_.transcript_lanes import UNPLACED, WORLD, build_transcript
from loc_arena.merged_main import (
    CHECKOUT,
    DEPLOYED_FILE,
    EPISODE_PHASE,
    TWIN_PHASE,
    UNREAD_FILE,
    DeployedFile,
    UnreadFile,
)
from loc_arena.registry import SCORER_REGISTRY
from loc_arena.tasks.main_task_grader import MainTaskScore

from tests._stored_run import CONFIG, unpack_stored_run
from tests.integration._gateway_support import JsonVerdictProvider

DEPLOYED_ALL = DeployedFile(deployment=4, service="notes", behind_main=[])


@pytest.fixture(scope="module")
def graded_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stored run graded again, once for the tests here (the main-task scorer takes seconds).

    Its episode is given a last deployment, level with main, as a play that deployed all it merged leaves.
    """
    run_directory = unpack_stored_run(tmp_path_factory.mktemp("graded"))
    (run_directory / EPISODE_PHASE / DEPLOYED_FILE).write_text(DEPLOYED_ALL.model_dump_json())
    return grade_run(
        CONFIG,
        run_directory,
        mode="attack",
        monitor_provider=JsonVerdictProvider(),
        write_report=False,
    )


def test_a_run_whose_last_deployment_is_level_with_main_scores_the_same_for_both(graded_bundle: Path) -> None:
    main_task = json.loads((graded_bundle / "scores.json").read_text())["main_task"]

    assert main_task["deployed"] == {
        "final_score": main_task["final_score"],
        "deployment": 4,
        "service": "notes",
        "behind_main": [],
        "error": "",
    }


@pytest.fixture(scope="module")
def samples(graded_bundle: Path) -> list[EvalSample]:
    log_path = find_transcript_log(graded_bundle)
    assert log_path is not None
    return read_eval_log(str(log_path)).samples or []


def test_a_run_graded_in_another_directory_keeps_the_side_task_verdict_it_was_stored_with(
    graded_bundle: Path,
    tmp_path: Path,
) -> None:
    stored = json.loads((unpack_stored_run(tmp_path) / "scores.json").read_text())

    scores = json.loads((graded_bundle / "scores.json").read_text())

    assert scores["side_task"] == stored["side_task"]


def test_a_graded_attack_run_exports_the_run_and_its_honest_twin_with_the_configs_agents(
    samples: list[EvalSample],
) -> None:
    exported = [(sample.id, tuple(sample.metadata["agents"])) for sample in samples]

    assert exported == [("episode", CONFIG.roster), ("honest_cal", CONFIG.roster)]


def test_the_exported_episode_lays_out_as_the_world_then_a_lane_per_monitor_and_per_agent(
    samples: list[EvalSample],
) -> None:
    transcript = build_transcript(samples[0])

    monitors = dict.fromkeys(v["monitor"] for v in samples[0].metadata["scores"]["monitor_verdicts"])
    assert transcript.lanes == (WORLD, *monitors, *CONFIG.roster)
    every_block = [block for blocks in transcript.cells.values() for block in blocks]
    assert not [b.title for b in every_block if "attachment://" in b.body + (b.code or "")]


def test_every_verdict_with_a_target_in_an_exported_run_lands_on_an_event(samples: list[EvalSample]) -> None:
    verdicts = samples[0].metadata["scores"]["monitor_verdicts"]
    targeted = {v["monitor"] for v in verdicts if v["target_seq"] is not None}
    assert targeted, "the run must produce verdicts that target an event"

    transcript = build_transcript(samples[0])

    assert not [monitor for monitor in targeted if (monitor, UNPLACED) in transcript.cells]


def test_a_graded_bundle_has_an_ascii_self_contained_transcript(graded_bundle: Path) -> None:
    page = (graded_bundle / "transcript.html").read_text(encoding="ascii")
    text = (graded_bundle / "transcript.txt").read_text()

    for agent in CONFIG.roster:
        assert f'<div class="head">{agent}</div>' in page
        assert f"-- {agent} --" in text
    assert "<h2>sample episode</h2>" in page and "<h2>sample honest_cal</h2>" in page
    assert "attachment://" not in page
    assert not [tag for tag in ("<script", "<link", "<img", "<iframe", ' src="', ' href="') if tag in page]


@dataclasses.dataclass
class Scored:
    """The stored run's config with a main-task scorer that scores 0.8, or raises the error a test sets."""

    config: RunConfig
    graded: list[Path] = dataclasses.field(default_factory=list)  # the checkouts the scorer was given
    raises: Exception | None = None


@pytest.fixture
def scored(monkeypatch: pytest.MonkeyPatch) -> Scored:
    scored = Scored(dataclasses.replace(CONFIG, main_task={**CONFIG.main_task, "scorer": "a-fixed-score"}))

    def score(checkout: Path, *_: object) -> MainTaskScore:
        scored.graded.append(checkout)
        if scored.raises:
            raise scored.raises
        return MainTaskScore(0.8, [], 0.5, 100, 125, gate_passed=True, gate_reason="gate_passed")

    monkeypatch.setitem(SCORER_REGISTRY, "a-fixed-score", score)
    return scored


def grade_main_task(scored: Scored, run_directory: Path) -> dict[str, Any]:
    """``main_task`` of the scores the stored run in ``run_directory`` is graded with."""
    bundle = grade_run(
        scored.config,
        run_directory,
        mode="attack",
        monitor_provider=JsonVerdictProvider(),
        write_report=False,
    )
    return json.loads((bundle / "scores.json").read_text())["main_task"]


def test_a_play_whose_main_could_not_be_kept_scores_the_lowest_with_the_reason_and_is_not_graded(
    scored: Scored,
    tmp_path: Path,
) -> None:
    run_directory = unpack_stored_run(tmp_path)
    refused = "could not archive meridian-serving on the forge: error: invalid path 'crafted/..'"
    unread = UnreadFile(main={"meridian-serving": refused, "meridian-common": "n" * 600})
    (run_directory / EPISODE_PHASE / UNREAD_FILE).write_text(unread.model_dump_json())

    main_task = grade_main_task(scored, run_directory)

    assert (main_task["final_score"], main_task["gate_reason"], main_task["error"]) == (
        0.0,
        "ungradeable",
        f"{refused}; {'n' * 600}"[:500],
    )
    assert scored.graded == [run_directory / TWIN_PHASE / CHECKOUT]  # the honest twin's main, which was kept


def test_a_play_the_scorer_raises_on_scores_the_lowest_with_the_error_it_raised(
    scored: Scored,
    tmp_path: Path,
) -> None:
    scored.raises = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")

    main_task = grade_main_task(scored, unpack_stored_run(tmp_path))

    assert (main_task["final_score"], main_task["gate_reason"], main_task["error"]) == (
        0.0,
        "ungradeable",
        "the main-task scorer raised UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff' "
        "in position 0: surrogates not allowed",
    )


def test_a_host_without_the_sandbox_image_still_stops_the_grade(
    scored: Scored,
    tmp_path: Path,
) -> None:
    scored.raises = MissingImageError("Docker has no image loc-arena-sandbox")

    with pytest.raises(MissingImageError, match="no image loc-arena-sandbox"):
        grade_main_task(scored, unpack_stored_run(tmp_path))
