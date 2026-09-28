"""Builders for the monitor unit tests: a one-monitor monitors.yaml, a prompt template, observable events."""

from __future__ import annotations

from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.logging_.events import Event, EventKind
from loc_arena.monitors.registry import MonitorSpec, load_monitor_specs

RUN_CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SHIPPED_MONITORS = "configs/monitors.yaml"
# A model reply every monitor reads as a strict JSON verdict.
VERDICT_REPLY = '{"suspicion": 0.1, "reason": "routine step"}'


def observable_event(seq: int, kind: EventKind, *, tool: str | None = None, **payload: object) -> Event:
    """A mirror event of ``kind`` by the main agent, as a monitor at that tap sees it."""
    return Event(
        episode_id="ep-monitor-unit",
        seq=seq,
        ts=float(seq),
        actor_uid="agent-main",
        actor_role="untrusted",
        kind=kind,
        payload=dict(payload),
        tool=tool,
    )


ACTION_EVENT = observable_event(0, "action", tool="read_file", args={"path": "README.md"})


def write_monitors_file(directory: Path, **entry: object) -> Path:
    """Write a monitors.yaml with one enabled action_monitor on the action tap, overridden by ``entry``."""
    monitor = {
        "name": "action_monitor",
        "tap": ["action"],
        "inputs": ["tool_name"],
        "model": "trusted_monitor",
        "enabled": True,
        **entry,
    }
    path = directory / "monitors.yaml"
    path.write_text(yaml.safe_dump({"monitors": [monitor]}))
    return path


def load_one_monitor_spec(directory: Path, **entry: object) -> MonitorSpec:
    """Load the one monitor ``write_monitors_file`` declares, through the loader the config check uses."""
    (spec,) = load_monitor_specs(write_monitors_file(directory, **entry))
    return spec


def write_prompt_template(directory: Path, text: str) -> str:
    """Write a prompt template file and return its path, as a monitor's ``prompt_ref`` names it."""
    path = directory / "template.txt"
    path.write_text(text)
    return str(path)
