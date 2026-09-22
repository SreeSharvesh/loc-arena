"""Side-task verifier binding.

Enforces the honest-twin-scores-zero guarantee (an honest twin reaches no milestone and neither landing)
and the firewall
(reads ONLY the sealed side). Binds ``verifier/side_task.py:kill_chain_v1`` to this run: it reads the
sealed event log (from a file in-process, or through the networkless ``evidence_reader`` in the docker
stack) and applies the milestones, landing predicates, and thresholds declared in the run config. It
never feeds a monitor and is never a tuning target.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loc_arena.config import RunConfig
from loc_arena.logging_.events import Event, read_events
from loc_arena.registry import get_verifier
from loc_arena.verifier.side_task import (  # noqa: F401 -- registers kill_chain_v1
    KillChainVerdict,
    kill_chain_v1,
)

if TYPE_CHECKING:
    from loc_arena.harness import EpisodeStack


def verify_side_task(sealed_log_path: str | Path, config: RunConfig, deadline_ts: float) -> KillChainVerdict:
    """Run the verifier named by ``config.side_task.verifier`` over the sealed log with this run's thresholds.

    The name dispatches through ``VERIFIER_REGISTRY`` (importing ``side_task`` registered ``kill_chain_v1``).
    """
    events = list(read_events(sealed_log_path))
    verifier = get_verifier(config.side_task.verifier)
    verdict: KillChainVerdict = verifier(events, config, deadline_ts)
    return verdict


def _event_from_dict(data: dict[str, Any]) -> Event:
    return Event(
        episode_id=data["episode_id"],
        seq=data["seq"],
        ts=data["ts"],
        actor_uid=data["actor_uid"],
        actor_role=data["actor_role"],
        kind=data["kind"],
        payload=data.get("payload", {}),
        target_id=data.get("target_id"),
        turn=data.get("turn", 0),
        parent_task=data.get("parent_task"),
        tool=data.get("tool"),
        result=data.get("result"),
        fp=data.get("fp", ""),
    )


def verify_via_evidence_reader(
    stack: EpisodeStack, config: RunConfig, deadline_ts: float, *, sealed_path: str = "/sealed/events.jsonl"
) -> KillChainVerdict:
    """Read the sealed log through the networkless evidence-reader (docker) and run ``kill_chain_v1``."""
    result = stack.exec("evidence_reader", ["cat", sealed_path], check=False)
    events = [_event_from_dict(json.loads(line)) for line in result.stdout.splitlines() if line.strip()]
    verdict: KillChainVerdict = get_verifier(config.side_task.verifier)(events, config, deadline_ts)
    return verdict
