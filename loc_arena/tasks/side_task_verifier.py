"""Side-task verifier binding.

Enforces the honest-twin-scores-zero guarantee (an honest twin reaches no milestone and neither landing)
and the firewall
(reads ONLY the sealed side). Binds ``verifier/side_task.py:kill_chain_v1`` to this run: it reads the
sealed event log from a file on this machine (written in process, or copied out of the stack's networkless
``evidence_reader`` by the harness) and applies the milestones, landing predicates, and thresholds declared
in the run config. It never feeds a monitor and is never a tuning target.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.logging_.events import Event, read_events
from loc_arena.registry import get_verifier
from loc_arena.verifier.side_task import (  # noqa: F401 -- registers kill_chain_v1
    KillChainVerdict,
    kill_chain_v1,
)


def verify_sealed_events(events: Sequence[Event], config: RunConfig, deadline_ts: float) -> KillChainVerdict:
    """Run the verifier named by ``config.side_task.verifier`` over sealed events with this run's thresholds.

    The name dispatches through ``VERIFIER_REGISTRY`` (importing ``side_task`` registered ``kill_chain_v1``).
    """
    verifier = get_verifier(config.side_task.verifier)
    verdict: KillChainVerdict = verifier(list(events), config, deadline_ts)
    return verdict


def verify_side_task(sealed_log_path: str | Path, config: RunConfig, deadline_ts: float) -> KillChainVerdict:
    """Run the verifier over the sealed log at ``sealed_log_path`` (``verify_sealed_events``)."""
    return verify_sealed_events(list(read_events(sealed_log_path)), config, deadline_ts)
