"""A deterministic run in the stack writes the in-process run's logs and scores, but for the wall clock."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.compose_stack import docker_available
from loc_arena.harness import load_config_by_name, run_episode
from loc_arena.stack_episode import run_in_stack

from tests.unit._golden import scores_without_wall_clock

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable"),
]

RUN = "aurora-efficiency.deterministic"
INSTANCE = "parity"  # one run name for both, so only the wall clock differs


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    in_process = run_episode(
        load_config_by_name(RUN),
        mode="attack",
        out_root=tmp_path_factory.mktemp("in-process"),
        run_instance_id=INSTANCE,
        write_report=False,
    )
    stacked = run_in_stack(
        RUN,
        "attack",
        out_root=tmp_path_factory.mktemp("stack"),
        run_instance_id=INSTANCE,
        dotenv_path=None,  # the deterministic run needs no provider key; read none
    )
    return in_process, stacked


@pytest.mark.parametrize("log_name", ["events.sealed.jsonl", "events.mirror.jsonl"])
def test_the_stack_writes_the_log_the_in_process_run_writes(
    bundles: tuple[Path, Path],
    log_name: str,
) -> None:
    in_process, stacked = bundles

    written = (stacked / log_name).read_bytes()

    assert written == (in_process / log_name).read_bytes()


def test_the_stack_scores_the_run_like_the_in_process_run(bundles: tuple[Path, Path]) -> None:
    in_process, stacked = bundles

    scores = scores_without_wall_clock(stacked / "scores.json")

    assert scores == scores_without_wall_clock(in_process / "scores.json")
