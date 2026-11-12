"""A live monitor's own model calls are sealed under its credential and count for no covert milestone."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from loc_arena.harness import apply_mode, grade_run
from loc_arena.logging_.events import Event, read_events
from loc_arena.merged_main import TWIN_PHASE
from loc_arena.task import read_snapshot
from loc_arena.tasks.side_task_verifier import verify_side_task

from tests._stored_run import CONFIG, unpack_stored_run
from tests.integration._gateway_support import JsonVerdictProvider
from tests.integration._records_support import SEALED

LIVE_MONITORS = {"action_monitor", "inference_monitor"}


def _monitors_file(directory: Path) -> Path:
    """The shipped monitors.yaml, ``LIVE_MONITORS`` live and the rest heuristic."""
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    for entry in data["monitors"]:
        entry["mode"] = "live" if entry["name"] in LIVE_MONITORS else "heuristic"
    path = directory / "monitors.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture(scope="module")
def stored_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return unpack_stored_run(tmp_path_factory.mktemp("stored"))


@pytest.fixture(scope="module")
def graded_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the stored run, graded with ``LIVE_MONITORS`` calling a stub model."""
    directory = tmp_path_factory.mktemp("graded")
    return grade_run(
        CONFIG,
        unpack_stored_run(directory),
        mode="attack",
        monitors_path=str(_monitors_file(directory)),
        monitor_provider=JsonVerdictProvider(),
        write_report=False,
    )


def _monitor_calls(events: list[Event]) -> list[Event]:
    return [e for e in events if e.kind == "inference_call" and e.payload.get("credential_kind") == "monitor"]


def test_grading_seals_each_live_monitors_model_calls_under_its_monitor_credential(
    stored_run: Path,
    graded_run: Path,
) -> None:
    sealed_before = len(list(read_events(stored_run / TWIN_PHASE / SEALED)))

    sealed_by_grading = list(read_events(graded_run / TWIN_PHASE / SEALED))[sealed_before:]

    assert {call.payload["caller_identity"] for call in _monitor_calls(sealed_by_grading)} == LIVE_MONITORS


def test_the_honest_twins_sealed_log_reaches_no_covert_milestone_with_monitor_calls_in_it(
    graded_run: Path,
) -> None:
    honest = apply_mode(CONFIG, "honest")
    twin = read_snapshot(graded_run / TWIN_PHASE, honest)

    verdict = verify_side_task(twin.sealed_path, honest, twin.deadline_ts)  # from the sealed log alone

    reached = [milestone for milestone, result in verdict.milestones.items() if result.reached]
    assert (verdict.landing.minimal, verdict.covert_depth, reached) == (False, 0, [])
