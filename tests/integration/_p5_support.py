"""Shared helpers for the monitor/metrics tests: configs, a wired protocol, and episode records."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import RunConfig, load_run_config
from loc_arena.metrics import EpisodeRecord, record_episode
from loc_arena.monitors.registry import load_monitors
from loc_arena.protocol import Protocol, compose_protocol
from loc_arena.task import assemble_scripted_episode

ATTACK_CFG: RunConfig = load_run_config("configs/aurora-efficiency.deterministic.yaml")
HONEST_CFG: RunConfig = dataclasses.replace(
    ATTACK_CFG,
    covert=dataclasses.replace(ATTACK_CFG.covert, enabled=False),
)


def protocol() -> Protocol:
    """Compose the protocol from the real monitors.yaml."""
    return compose_protocol(ATTACK_CFG, load_monitors("configs/monitors.yaml", ATTACK_CFG.models))


def honest_record(tmp_path: Path, proto: Protocol) -> EpisodeRecord:
    """Assemble and score an honest twin episode."""
    return record_episode(assemble_scripted_episode(HONEST_CFG, tmp_path), HONEST_CFG, proto)


def attack_record(tmp_path: Path, proto: Protocol, *, robust: bool) -> EpisodeRecord:
    """Assemble and score an attack episode (minimal or robust)."""
    return record_episode(assemble_scripted_episode(ATTACK_CFG, tmp_path, robust=robust), ATTACK_CFG, proto)
