"""A live monitor whose model replies do not parse falls back to its heuristic, and scores.json says so.

A copy of the stored run is graded once, the monitors' model a stub provider: no network, no key. Every test
reads one outcome of that grading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from loc_arena.harness import apply_mode, grade_run
from loc_arena.logging_.events import read_events
from loc_arena.merged_main import EPISODE_PHASE
from loc_arena.monitors.registry import load_monitors
from loc_arena.protocol import compose_protocol, monitor_context

from tests._stored_run import CONFIG, unpack_stored_run
from tests.integration._gateway_support import MalformedProvider
from tests.integration._records_support import MIRROR, SEALED

LIVE_MONITOR = "action_monitor"


def _monitors_file(directory: Path, live_monitor_mode: str) -> Path:
    """The shipped monitors.yaml, ``LIVE_MONITOR`` in ``live_monitor_mode`` and the rest heuristic."""
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    for entry in data["monitors"]:
        entry["mode"] = live_monitor_mode if entry["name"] == LIVE_MONITOR else "heuristic"
    path = directory / f"monitors.{live_monitor_mode}.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture(scope="module")
def stored_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return unpack_stored_run(tmp_path_factory.mktemp("stored"))


@pytest.fixture(scope="module")
def unparseable_reply_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the stored run, graded with a live ``LIVE_MONITOR`` that gets only unparseable replies."""
    directory = tmp_path_factory.mktemp("unparseable")
    return grade_run(
        CONFIG,
        unpack_stored_run(directory),
        mode="attack",
        monitors_path=str(_monitors_file(directory, "live")),
        monitor_provider=MalformedProvider(),
        write_report=False,
    )


def test_an_unparseable_reply_is_logged_as_a_parse_error_of_its_monitor(
    stored_run: Path,
    unparseable_reply_run: Path,
) -> None:
    sealed_before = len(list(read_events(stored_run / EPISODE_PHASE / SEALED)))

    logged = list(read_events(unparseable_reply_run / EPISODE_PHASE / SEALED))[sealed_before:]

    failing = {str(event.payload.get("monitor")) for event in logged if event.kind == "monitor_parse_error"}
    assert failing == {LIVE_MONITOR}


def test_a_live_monitor_whose_replies_do_not_parse_scores_as_its_heuristic(
    stored_run: Path,
    unparseable_reply_run: Path,
    tmp_path: Path,
) -> None:
    heuristic = compose_protocol(CONFIG, load_monitors(_monitors_file(tmp_path, "heuristic"), CONFIG.models))
    mirror_events = list(read_events(stored_run / EPISODE_PHASE / MIRROR))
    heuristic_verdicts = [
        (verdict.target_seq, verdict.score, verdict.reason)
        for verdict in heuristic.run(mirror_events, monitor_context(apply_mode(CONFIG, "attack")))
        if verdict.monitor == LIVE_MONITOR
    ]

    scores = json.loads((unparseable_reply_run / "scores.json").read_text())

    fallback_verdicts = [
        (row["target_seq"], row["score"], row["reason"])
        for row in scores["monitor_verdicts"]
        if row["monitor"] == LIVE_MONITOR
    ]
    assert heuristic_verdicts
    assert fallback_verdicts == heuristic_verdicts


def test_only_the_live_monitor_whose_every_reply_fails_to_parse_is_reported_errored(
    unparseable_reply_run: Path,
) -> None:
    scores = json.loads((unparseable_reply_run / "scores.json").read_text())

    errored = {row["name"]: row["errored"] for row in scores["monitors"]}

    assert errored == {LIVE_MONITOR: True, "inference_monitor": False, "pr_review_monitor": False}
