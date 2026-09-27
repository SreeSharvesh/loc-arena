from __future__ import annotations

import json
from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.harness import run_episode
from loc_arena.logging_.events import read_events

from tests.integration._gateway_support import JsonVerdictProvider, MalformedProvider

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _live_action_yaml(tmp_path: Path) -> Path:
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    # Control the live set exactly (default ships all model monitors live): only action_monitor here.
    for entry in data["monitors"]:
        entry["mode"] = "live" if entry["name"] == "action_monitor" else "heuristic"
    out = tmp_path / "monitors.yaml"
    out.write_text(yaml.safe_dump(data))
    return out


def test_malformed_completion_logs_error_and_falls_back(tmp_path: Path) -> None:
    monitors = _live_action_yaml(tmp_path)
    bundle = run_episode(
        CFG,
        mode="attack",
        out_root=tmp_path,
        robust=True,
        monitors_path=str(monitors),
        monitor_provider=MalformedProvider(),
    )
    events = list(read_events(bundle / "events.sealed.jsonl"))
    # the parse failure was recorded and the episode still completed with a full bundle
    assert any(e.kind == "monitor_parse_error" for e in events)
    assert (bundle / "scores.json").exists() and (bundle / "report.html").exists()

    # action_monitor fell back to its deterministic heuristic (not a live verdict)

    scores = json.loads((bundle / "scores.json").read_text())
    action_reasons = {v["reason"] for v in scores["monitor_verdicts"] if v["monitor"] == "action_monitor"}
    assert action_reasons <= {"routine coordination", "job submission", "routine action"}

    # ... and scores.json says so: every score it reported is the fallback, so it is errored
    errored = _errored_by_monitor(scores)
    assert errored["action_monitor"] is True
    assert errored["pr_review_monitor"] is False  # heuristic by config: it never called the model


def test_parsed_live_verdicts_are_not_errored(tmp_path: Path) -> None:
    bundle = run_episode(
        CFG,
        mode="attack",
        out_root=tmp_path,
        robust=True,
        monitors_path=str(_live_action_yaml(tmp_path)),
        monitor_provider=JsonVerdictProvider(suspicion=0.2, reason="stub verdict"),
    )
    scores = json.loads((bundle / "scores.json").read_text())
    assert _errored_by_monitor(scores)["action_monitor"] is False


def _errored_by_monitor(scores: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return {str(row["name"]): row["errored"] for row in scores["monitors"]}
