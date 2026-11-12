"""The shipped monitors score the events of a played episode."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.logging_.events import read_events
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE
from loc_arena.monitors.registry import load_monitors
from loc_arena.protocol import compose_protocol, monitor_context

from tests._stored_run import CONFIG, unpack_stored_run
from tests.integration._records_support import MIRROR


@pytest.fixture(scope="module")
def stored_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return unpack_stored_run(tmp_path_factory.mktemp("stored"))


@pytest.mark.parametrize("phase", [EPISODE_PHASE, TWIN_PHASE])
def test_every_heuristic_verdict_on_a_played_episode_is_a_reasoned_score_naming_its_monitor_and_tap(
    stored_run: Path,
    phase: str,
) -> None:
    protocol = compose_protocol(CONFIG, load_monitors("configs/monitors.yaml", CONFIG.models))
    mirror_events = list(read_events(stored_run / phase / MIRROR))

    verdicts = protocol.run(mirror_events, monitor_context(CONFIG))

    assert verdicts
    assert all(0.0 <= v.score <= 1.0 and v.reason and v.monitor and v.tap for v in verdicts)
