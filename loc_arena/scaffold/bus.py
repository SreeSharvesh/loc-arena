"""Dual capture: every observable event goes to the sealed log and the mirror log.

The sealed log is the ground truth the agents cannot reach; the mirror log is what a monitor reads, and their
divergence is a signal. Every event is fingerprinted on an append-only log. Messages between agents travel on
the board (``loc_arena.board``), and their events are built from its records.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from loc_arena.logging_.events import AppendOnlyLog, Event, EventKind


class Recorder:
    """Writes events to the sealed log (ground truth) and, for observable events, the mirror log too.

    ``sealed`` writes only to the sealed log (lifecycle events the monitor does not read: spawn/close).
    ``dual`` writes the same logical event to both, so a monitor-observable action or message lands on the
    mirror as well. Each write gets its own monotonic seq in its file; reconstruction reads the sealed
    log.
    """

    def __init__(
        self,
        episode_id: str,
        sealed: AppendOnlyLog,
        mirror: AppendOnlyLog,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the recorder to its episode and the two append-only logs."""
        self._episode_id = episode_id
        self._sealed = sealed
        self._mirror = mirror
        self._clock = clock

    def _build(
        self,
        log: AppendOnlyLog,
        *,
        ts: float,
        actor_uid: str,
        actor_role: str,
        kind: EventKind,
        payload: dict[str, Any],
        target_id: str | None,
        turn: int,
        tool: str | None,
        result: dict[str, Any] | None,
    ) -> Event:
        return Event(
            episode_id=self._episode_id,
            seq=log.next_seq(),
            ts=ts,
            actor_uid=actor_uid,
            actor_role=actor_role,
            kind=kind,
            payload=payload,
            target_id=target_id,
            turn=turn,
            tool=tool,
            result=result,
        )

    def sealed(
        self,
        *,
        actor_uid: str,
        actor_role: str,
        kind: EventKind,
        payload: dict[str, Any],
        target_id: str | None = None,
        turn: int = 0,
        tool: str | None = None,
        result: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> Event:
        """Append one event to the sealed log only (ground-truth lifecycle); return the written event."""
        return self._sealed.append(
            self._build(
                self._sealed,
                ts=ts if ts is not None else self._clock(),
                actor_uid=actor_uid,
                actor_role=actor_role,
                kind=kind,
                payload=payload,
                target_id=target_id,
                turn=turn,
                tool=tool,
                result=result,
            ),
        )

    def dual(
        self,
        *,
        actor_uid: str,
        actor_role: str,
        kind: EventKind,
        payload: dict[str, Any],
        target_id: str | None = None,
        turn: int = 0,
        tool: str | None = None,
        result: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> Event:
        """Append one observable event to BOTH the sealed and the mirror log; return the sealed event."""
        at = ts if ts is not None else self._clock()
        sealed_event = self.sealed(
            actor_uid=actor_uid,
            actor_role=actor_role,
            kind=kind,
            payload=payload,
            target_id=target_id,
            turn=turn,
            tool=tool,
            result=result,
            ts=at,
        )
        self._mirror.append(
            self._build(
                self._mirror,
                ts=at,
                actor_uid=actor_uid,
                actor_role=actor_role,
                kind=kind,
                payload=payload,
                target_id=target_id,
                turn=turn,
                tool=tool,
                result=result,
            ),
        )
        return sealed_event
