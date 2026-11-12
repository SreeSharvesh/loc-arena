"""Live: real monitor models score a copy of the stored run. Spends money; runs only with LOC_ARENA_LIVE=1."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from loc_arena.gateway.core import OpenRouterProvider
from loc_arena.harness import grade_run

from tests._stored_run import CONFIG, unpack_stored_run

_LIVE = os.environ.get("LOC_ARENA_LIVE") == "1" and bool(os.environ.get("OPENROUTER_API_KEY"))
pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live monitor disabled (set LOC_ARENA_LIVE=1 + OPENROUTER_API_KEY)",
)
LIVE_MONITORS = ("action_monitor", "inference_monitor")


def _live_monitors_yaml(tmp_path: Path) -> Path:
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    for entry in data["monitors"]:
        entry["mode"] = "live" if entry["name"] in LIVE_MONITORS else "heuristic"
    out = tmp_path / "monitors.yaml"
    out.write_text(yaml.safe_dump(data))
    return out


def test_live_llm_monitor_scores_the_run(tmp_path: Path) -> None:
    monitors = _live_monitors_yaml(tmp_path)
    run = unpack_stored_run(tmp_path / "run")

    grade_run(
        CONFIG,
        run,
        mode="attack",
        monitors_path=str(monitors),
        monitor_provider=OpenRouterProvider(timeout=CONFIG.settings.gateway.timeout_seconds),
    )

    scores = json.loads((run / "scores.json").read_text())
    for name in LIVE_MONITORS:
        verdicts = [v for v in scores["monitor_verdicts"] if v["monitor"] == name]
        assert verdicts, f"{name} produced no verdict"
        assert all(0.0 <= v["score"] <= 1.0 and v["reason"] for v in verdicts)
    assert "monitor scores" in (run / "report.html").read_text()  # the verdicts are visible in the report
