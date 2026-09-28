"""A live monitor whose model replies do not parse falls back to its heuristic, and scores.json says so.

The episodes are scripted and the model is a stub provider: no network, no key. Each module-scoped fixture
runs its episode once, and every test reads one outcome from the bundle it wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from loc_arena.config import load_run_config
from loc_arena.gateway.core import Provider
from loc_arena.harness import run_episode
from loc_arena.logging_.events import read_events

from tests.integration._gateway_support import AlternatingVerdictProvider, MalformedProvider

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
LIVE_MONITOR = "action_monitor"
HEURISTIC_MONITOR = "pr_review_monitor"
# What an episode writes once its monitors have scored it.
GRADED_OUTPUTS = frozenset({"scores.json", "report.html"})


def _monitors_file(directory: Path, live_monitor_mode: str) -> Path:
    """The shipped monitors.yaml, ``LIVE_MONITOR`` in ``live_monitor_mode`` and the rest heuristic."""
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    for entry in data["monitors"]:
        entry["mode"] = live_monitor_mode if entry["name"] == LIVE_MONITOR else "heuristic"
    path = directory / "monitors.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _run_attack_episode(directory: Path, live_monitor_mode: str, monitor_provider: Provider | None) -> Path:
    """Run the scripted attack episode with ``monitor_provider`` as the monitors' model; its bundle."""
    return run_episode(
        CFG,
        mode="attack",
        out_root=directory,
        robust=True,
        monitors_path=str(_monitors_file(directory, live_monitor_mode)),
        monitor_provider=monitor_provider,
    )


def _verdict_rows(bundle: Path, monitor: str) -> list[tuple[object, object, object]]:
    """A monitor's verdicts in the bundle's scores.json, as (target_seq, score, reason), in order."""
    scores = json.loads((bundle / "scores.json").read_text())
    return [
        (row["target_seq"], row["score"], row["reason"])
        for row in scores["monitor_verdicts"]
        if row["monitor"] == monitor
    ]


def _errored_by_monitor(bundle: Path) -> dict[str, object]:
    """Each monitor's ``errored`` flag in the bundle's scores.json."""
    scores = json.loads((bundle / "scores.json").read_text())
    return {str(row["name"]): row["errored"] for row in scores["monitors"]}


@pytest.fixture(scope="module")
def unparseable_reply_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The bundle of an attack episode whose live ``LIVE_MONITOR`` gets only unparseable replies."""
    return _run_attack_episode(tmp_path_factory.mktemp("unparseable"), "live", MalformedProvider())


@pytest.fixture(scope="module")
def heuristic_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The same attack episode with ``LIVE_MONITOR`` in heuristic mode: the scores its fallback must match."""
    return _run_attack_episode(tmp_path_factory.mktemp("heuristic"), "heuristic", None)


def test_an_unparseable_reply_is_logged_as_a_parse_error_of_its_monitor(
    unparseable_reply_bundle: Path,
) -> None:
    events = read_events(unparseable_reply_bundle / "events.sealed.jsonl")

    failing = {str(event.payload.get("monitor")) for event in events if event.kind == "monitor_parse_error"}

    assert failing == {LIVE_MONITOR}


def test_an_episode_whose_monitor_replies_do_not_parse_still_writes_its_graded_outputs(
    unparseable_reply_bundle: Path,
) -> None:
    written = {path.name for path in unparseable_reply_bundle.iterdir()}

    assert GRADED_OUTPUTS <= written


def test_a_live_monitor_whose_replies_do_not_parse_scores_as_its_heuristic(
    unparseable_reply_bundle: Path,
    heuristic_bundle: Path,
) -> None:
    heuristic_verdicts = _verdict_rows(heuristic_bundle, LIVE_MONITOR)
    assert heuristic_verdicts, f"the heuristic episode has no {LIVE_MONITOR} verdict to compare against"

    fallback_verdicts = _verdict_rows(unparseable_reply_bundle, LIVE_MONITOR)

    assert fallback_verdicts == heuristic_verdicts


def test_a_live_monitor_whose_every_reply_fails_to_parse_is_reported_errored(
    unparseable_reply_bundle: Path,
) -> None:
    errored = _errored_by_monitor(unparseable_reply_bundle)

    assert errored[LIVE_MONITOR] is True


def test_a_heuristic_monitor_beside_a_failing_live_one_is_not_reported_errored(
    unparseable_reply_bundle: Path,
) -> None:
    errored = _errored_by_monitor(unparseable_reply_bundle)

    assert errored[HEURISTIC_MONITOR] is False


def test_a_live_monitor_with_a_parsed_reply_among_unparseable_ones_is_not_reported_errored(
    tmp_path: Path,
) -> None:
    # The attack episode scores three actions after its honest twin, so the alternating replies give its
    # live monitor both a parse error and a verdict from the model.
    provider = AlternatingVerdictProvider()

    bundle = _run_attack_episode(tmp_path, "live", provider)

    assert _errored_by_monitor(bundle)[LIVE_MONITOR] is False
