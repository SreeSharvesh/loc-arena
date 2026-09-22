from __future__ import annotations

from pathlib import Path

import yaml
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import MonitorContext, load_monitors

from tests.integration._p5_support import ATTACK_CFG

_THRESHOLDS = ATTACK_CFG.side_task.thresholds
_BASELINE = frozenset({"agent-main"})


def _action_event() -> Event:
    return Event(
        episode_id="e",
        seq=1,
        ts=1.0,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind="action",
        tool="submit_job",
        payload={"args": {}, "target": None},
    )


def _monitors_yaml_with_mode(tmp_path: Path, monitor: str, mode: str) -> Path:
    data = yaml.safe_load(Path("configs/monitors.yaml").read_text())
    for entry in data["monitors"]:
        if entry["name"] == monitor:
            entry["mode"] = mode
    out = tmp_path / "monitors.yaml"
    out.write_text(yaml.safe_dump(data))
    return out


def test_heuristic_default_does_not_call_the_model(tmp_path: Path) -> None:
    calls: list[str] = []

    def spy(name: str, prompt: str, temperature: float) -> str:
        calls.append(name)
        return '{"suspicion": 0.9, "reason": "x"}'

    reg = load_monitors(_monitors_yaml_with_mode(tmp_path, "action_monitor", "heuristic"), ATTACK_CFG.models)
    ctx = MonitorContext("optimize", _BASELINE, _THRESHOLDS, model_caller=spy)
    verdicts = reg.by_name("action_monitor").run([_action_event()], ctx)
    assert calls == []  # heuristic mode never touches the model
    assert verdicts and verdicts[0].reason == "job submission"  # the heuristic score


def test_live_mode_calls_the_model_no_code_change(tmp_path: Path) -> None:
    calls: list[str] = []

    def spy(name: str, prompt: str, temperature: float) -> str:
        calls.append(name)
        return '{"suspicion": 0.7, "reason": "live verdict"}'

    reg = load_monitors(_monitors_yaml_with_mode(tmp_path, "action_monitor", "live"), ATTACK_CFG.models)
    ctx = MonitorContext("optimize", _BASELINE, _THRESHOLDS, model_caller=spy)
    verdicts = reg.by_name("action_monitor").run([_action_event()], ctx)
    assert calls == ["action_monitor"]  # flipping the config to live made it call the model, no code change
    assert verdicts[0].score == 0.7 and verdicts[0].reason == "live verdict"
