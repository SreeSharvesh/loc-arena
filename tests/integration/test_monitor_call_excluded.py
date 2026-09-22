from __future__ import annotations

from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.harness import run_episode
from loc_arena.logging_.events import read_events
from loc_arena.tasks.side_task_verifier import verify_side_task

from tests.integration._gateway_support import JsonVerdictProvider

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
DEADLINE = 10_000.0


def _live_monitors_yaml(tmp_path: Path) -> Path:
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    # This test focuses on exactly action_monitor + inference_monitor live, so pin the others heuristic
    # regardless of the
    # repo default (monitors.yaml ships them all live for the deliverable run; here we control the set).
    for entry in data["monitors"]:
        entry["mode"] = "live" if entry["name"] in ("action_monitor", "inference_monitor") else "heuristic"
    out = tmp_path / "monitors.yaml"
    out.write_text(yaml.safe_dump(data))
    return out


def test_monitor_calls_excluded_and_honest_twin_stays_clean(tmp_path: Path) -> None:
    monitors = _live_monitors_yaml(tmp_path)
    bundle = run_episode(
        CFG,
        mode="honest",
        out_root=tmp_path,
        monitors_path=str(monitors),
        monitor_provider=JsonVerdictProvider(),
    )
    events = list(read_events(bundle / "events.sealed.jsonl"))

    # the monitors' own model calls are in the sealed log, tagged with the monitor credential (attributable)
    monitor_calls = [
        e for e in events if e.kind == "inference_call" and e.payload.get("credential_kind") == "monitor"
    ]
    assert monitor_calls  # live monitors ran and their calls were logged
    assert all(e.payload["caller_identity"] in {"action_monitor", "inference_monitor"} for e in monitor_calls)

    # re-verifying the sealed log (now containing monitor calls) still scores the honest twin CLEAN
    verdict = verify_side_task(bundle / "events.sealed.jsonl", CFG, DEADLINE)
    assert verdict.landing.minimal is False
    assert verdict.landing.robust is False
    assert verdict.covert_depth == 0


def test_live_monitors_do_not_change_the_attack_verdict(tmp_path: Path) -> None:
    monitors = _live_monitors_yaml(tmp_path)
    bundle = run_episode(
        CFG,
        mode="attack",
        out_root=tmp_path,
        robust=True,
        monitors_path=str(monitors),
        monitor_provider=JsonVerdictProvider(),
    )
    # the agent's off-path loop still lands robust; the monitor calls (excluded) do not perturb it
    verdict = verify_side_task(bundle / "events.sealed.jsonl", CFG, DEADLINE)
    assert verdict.landing.robust is True
