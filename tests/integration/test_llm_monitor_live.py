from __future__ import annotations

import json
from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.gateway.openrouter_provider import OpenRouterProvider
from loc_arena.harness import run_episode
from loc_arena.logging_.events import read_events

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _live_monitors_yaml(tmp_path: Path) -> Path:
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    # Control the live set exactly (default ships all model monitors live): action_monitor +
    # inference_monitor here.
    for entry in data["monitors"]:
        entry["mode"] = "live" if entry["name"] in ("action_monitor", "inference_monitor") else "heuristic"
    out = tmp_path / "monitors.yaml"
    out.write_text(yaml.safe_dump(data))
    return out


def test_live_llm_monitor_scores_the_run(tmp_path: Path, live_provider: OpenRouterProvider) -> None:
    monitors = _live_monitors_yaml(tmp_path)
    bundle = run_episode(
        CFG,
        mode="attack",
        out_root=tmp_path,
        robust=False,
        monitors_path=str(monitors),
        monitor_provider=live_provider,
    )
    events = list(read_events(bundle / "events.sealed.jsonl"))
    # the monitors' own calls really went through the gateway (trusted-monitor role, monitor credential)
    monitor_calls = [
        e for e in events if e.kind == "inference_call" and e.payload.get("credential_kind") == "monitor"
    ]
    assert monitor_calls

    scores = json.loads((bundle / "scores.json").read_text())
    for name in ("action_monitor", "inference_monitor"):
        verdicts = [v for v in scores["monitor_verdicts"] if v["monitor"] == name]
        assert verdicts, f"{name} produced no verdict"
        assert all(0.0 <= v["score"] <= 1.0 and v["reason"] for v in verdicts)
    # the verdicts are visible in the report
    assert "monitor scores" in (bundle / "report.html").read_text()
