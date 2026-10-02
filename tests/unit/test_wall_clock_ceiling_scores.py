"""A run the wall-clock ceiling stopped says so in its bundle; a run it did not stop writes what it did."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.evidence import read_runner_export
from loc_arena.explorer.build import build_run_page
from loc_arena.explorer.settings import ExplorerSettings
from loc_arena.gateway.core import DeterministicProvider
from loc_arena.harness import apply_mode, run_episode
from loc_arena.stack.constants import EVENTS_FILE_NAME, RUNNER_EPISODE_EXPORT_FILE_NAME
from loc_arena.stack_episode import _grade_on_host

from tests.unit._stack_services import serve_stack

LIVE = dataclasses.replace(load_run_config("configs/aurora-efficiency.deterministic.yaml"), policy="model")
PASSED_CEILING_SECONDS = 0  # the ceiling has passed before the first turn
UNMEASURED_CHECKOUT = b""  # no grader ran: the host grades the checkout as a failed measurement
STOPPED = dataclasses.replace(
    LIVE,
    episode=dataclasses.replace(LIVE.episode, wall_clock_seconds=PASSED_CEILING_SECONDS),
)


def _run_bundle(config: RunConfig, out_root: Path) -> Path:
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("OPENROUTER_API_KEY", raising=False)
        return run_episode(
            config,
            mode="attack",
            out_root=out_root,
            provider=DeterministicProvider(),
            calibration_provider=DeterministicProvider(),
            write_report=False,
        )


@pytest.fixture(scope="module")
def stopped_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run_bundle(STOPPED, tmp_path_factory.mktemp("stopped"))


def test_a_run_stopped_at_the_wall_clock_ceiling_records_it_in_its_scores(stopped_bundle: Path) -> None:
    scores = json.loads((stopped_bundle / "scores.json").read_text())

    assert scores["wall_clock_ceiling"] == {
        "seconds": PASSED_CEILING_SECONDS,
        "episode_stopped": True,
        "calibration_stopped": True,
    }


def test_a_run_the_wall_clock_ceiling_did_not_stop_writes_no_ceiling_in_its_scores(tmp_path: Path) -> None:
    scores = json.loads((_run_bundle(LIVE, tmp_path) / "scores.json").read_text())

    assert "wall_clock_ceiling" not in scores


def test_the_explorer_page_of_a_run_stopped_at_the_ceiling_shows_the_stop(stopped_bundle: Path) -> None:
    page = build_run_page(stopped_bundle, ExplorerSettings())

    assert page.run.wall_clock_ceiling is not None


def test_the_stack_runner_carries_a_stop_at_the_ceiling_to_the_host(tmp_path: Path) -> None:
    config = apply_mode(STOPPED, "attack")
    served = serve_stack(tmp_path, config, provider=DeterministicProvider())
    max_bytes = config.settings.docker.evidence_max_bytes

    served.run_runner(config, tmp_path)
    export = read_runner_export(tmp_path / "runner" / RUNNER_EPISODE_EXPORT_FILE_NAME, max_bytes)
    episode = _grade_on_host(
        config,
        export,
        UNMEASURED_CHECKOUT,
        sealed_directory=served.gateway.sealed_root / export.handle,
        mirror_path=served.gateway.mirror_root / export.handle / EVENTS_FILE_NAME,
    )

    assert episode.stopped_at_wall_clock_ceiling is True
