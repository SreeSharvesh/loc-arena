"""Sessions: peers an agent starts in play, each with an identity of its own (docs/pi-harness.md#sessions)."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.config import load_run_config


@pytest.mark.parametrize(
    ("overlay", "identity"),
    [
        ("", "approval"),  # the aurora scenario's, over the engine's default of refused
        ("sessions:\n  identity: automatic\n", "automatic"),  # the run config's, over the scenario's
    ],
)
def test_a_scenario_sets_how_sessions_are_approved_and_a_run_config_overrides_it(
    tmp_path: Path,
    overlay: str,
    identity: str,
) -> None:
    (tmp_path / "run.yaml").write_text(f"extends: aurora-efficiency.yaml\n{overlay}")

    config = load_run_config(tmp_path / "run.yaml", Path("configs"))

    assert config.settings.sessions.identity == identity
