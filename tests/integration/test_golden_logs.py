"""Sealed and mirror logs of three deterministic episodes match the stored golden files line by line.

``test_reproducible_from_config_and_seed`` proves two runs agree with each other; this pins what they agree
on, so a refactor of the gateway or the logging cannot change a record unnoticed. The scripted episodes
cover the prompt, batch, durable-credential and monitor paths; the queued live episode covers chat
messages and native tool calls.

An intended change to the logs is made by rerunning with ``LOC_ARENA_UPDATE_GOLDEN=1`` and reviewing the
diff of ``tests/integration/golden/``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode, run_episode
from loc_arena.live import assemble_model_episode

from tests.integration._live_support import LANDING_QUEUES, QueuedProvider

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("LOC_ARENA_UPDATE_GOLDEN") == "1"
LOGS = ("events.sealed.jsonl", "events.mirror.jsonl")


def _scripted_attack(out: Path) -> Path:
    return run_episode(CFG, mode="attack", out_root=out, robust=True)


def _scripted_honest(out: Path) -> Path:
    return run_episode(CFG, mode="honest", out_root=out)


def _live_queued_attack(out: Path) -> Path:
    ep = assemble_model_episode(
        apply_mode(CFG, "attack"),
        out,
        provider=QueuedProvider(LANDING_QUEUES),
        deadline_ts=10_000.0,
    )
    return ep.sealed_path.parent


EPISODES: dict[str, Callable[[Path], Path]] = {
    "scripted-attack": _scripted_attack,
    "scripted-honest": _scripted_honest,
    "live-queued-attack": _live_queued_attack,
}


@pytest.mark.parametrize("name", EPISODES)
def test_logs_match_golden(name: str, tmp_path: Path) -> None:
    log_dir = EPISODES[name](tmp_path)

    for log in LOGS:
        actual = (log_dir / log).read_text()
        golden = GOLDEN / f"{name}.{log}"
        if UPDATE:
            golden.parent.mkdir(exist_ok=True)
            golden.write_text(actual)
        assert actual.splitlines() == golden.read_text().splitlines(), f"{golden.name} changed"
