"""Side-task verifier binding.

Enforces the honest-twin-scores-zero guarantee (an honest twin reaches no milestone and neither landing)
and the firewall
(reads ONLY the sealed side). Binds ``verifier/side_task.py:kill_chain_v1`` to this run: it reads the
sealed event log and applies the milestones, landing predicates, and thresholds declared in the run config. It
never feeds a monitor and is never a tuning target.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loc_arena.config import RunConfig
from loc_arena.logging_.events import read_events
from loc_arena.registry import get_verifier
from loc_arena.verifier.side_task import (  # noqa: F401 -- registers kill_chain_v1
    KillChainVerdict,
    kill_chain_v1,
)

if TYPE_CHECKING:  # the reader imports loc_arena.task, which imports this module
    from loc_arena.verifier.call_record import RecordedCalls


def verify_side_task(
    sealed_log_path: str | Path,
    config: RunConfig,
    deadline_ts: float,
    calls: RecordedCalls | None = None,
) -> KillChainVerdict:
    """Run the verifier named by ``config.side_task.verifier`` over the sealed log with this run's thresholds.

    The name dispatches through ``VERIFIER_REGISTRY`` (importing ``side_task`` registered ``kill_chain_v1``).
    A stack run's play also has its model ``calls`` from the gateway's call record, since a job's calls land
    there alone; the play's end is then the deadline.
    """
    events = list(read_events(sealed_log_path))
    verifier = get_verifier(config.side_task.verifier)
    deadline = calls.deadline if calls else deadline_ts
    verdict: KillChainVerdict = verifier(events, config, deadline, calls=calls)
    return verdict
