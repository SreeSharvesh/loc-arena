from __future__ import annotations

from pathlib import Path

import yaml
from loc_arena.monitors.registry import load_monitors

from tests.integration._p5_support import ATTACK_CFG

_MODELS = ATTACK_CFG.models


def test_enabled_monitors_are_wired_to_their_taps() -> None:
    reg = load_monitors("configs/monitors.yaml", _MODELS)
    names = {m.name for m in reg.enabled()}
    assert names == {
        "action_monitor",
        "inference_monitor",
        "pr_review_monitor",
        "monitor_config_reconcile",
        "logging_config_reconcile",
    }
    # action_monitor is attached to BOTH the action and the message taps
    assert reg.by_name("action_monitor") in reg.for_tap("action")
    assert reg.by_name("action_monitor") in reg.for_tap("message")
    assert reg.by_name("inference_monitor") in reg.for_tap("inference")
    assert reg.by_name("pr_review_monitor") in reg.for_tap("pr")


def test_disabled_monitor_is_not_wired() -> None:
    reg = load_monitors("configs/monitors.yaml", _MODELS)
    assert "shared_surface_monitor" not in {m.name for m in reg.enabled()}  # enabled: false in monitors.yaml
    assert reg.for_tap("shared_surface") == []


def test_moving_a_monitor_in_config_moves_where_it_fires(tmp_path: Path) -> None:
    # move action_monitor from [action, message] to [message] with a config edit -- no code change
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    for entry in data["monitors"]:
        if entry["name"] == "action_monitor":
            entry["tap"] = ["message"]
    moved = tmp_path / "monitors.yaml"
    moved.write_text(yaml.safe_dump(data))
    reg = load_monitors(moved, _MODELS)
    assert reg.by_name("action_monitor") in reg.for_tap("message")
    assert reg.by_name("action_monitor") not in reg.for_tap("action")
