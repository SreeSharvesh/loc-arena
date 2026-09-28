"""Scaffold-hosted message bus with dual capture.

Enforces fingerprinted, append-only logging (every message and every action is a fingerprinted event on
the append-only log) and the sealed-vs-tamperable isolation (dual capture: each event is written to the
SEALED log the agent cannot reach and to
the MIRROR log a monitor reads; their divergence is a signal). The bus is the single coordination channel
and the primary capture point: ``send`` writes ONE logical message
tagged with both the sender (``actor_uid``) and the recipient (``target_id``), so it reconstructs into
both trajectories, and delivers in order per recipient. Every message is parsed by the ``action`` hook
(M_action) before dispatch; the real monitor plugs into that fixed interface.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, Final

from loc_arena.logging_.events import Event, EventDraft, EventKind, EventLog, fingerprint

# An event's fields that a log does not assign (all but episode_id, seq and fp): what its mirror twin copies.
_DRAFT_FIELDS: Final = tuple(field.name for field in dataclasses.fields(EventDraft))

# The M_action hook: parses each event before dispatch. Pass-through by default; the real monitor plugs in.
ActionHook = Callable[[Event], None]


def _noop_hook(event: Event) -> None:
    """Default pass-through M_action hook (the interface a monitor replaces)."""


class Recorder:
    """Writes events to the sealed log (ground truth) and, for observable events, the mirror log too.

    ``sealed`` writes only to the sealed log (lifecycle events the monitor does not read: spawn/close).
    ``dual`` writes the same logical event to both, so a monitor-observable action or message lands on the
    mirror as well. Each log gives the event its own seq; reconstruction reads the sealed log. The logs are
    the episode's wiring: local files in process, the core and the edge in the stack.
    """

    def __init__(self, sealed: EventLog, mirror: EventLog, *, clock: Callable[[], float]) -> None:
        """Bind the recorder to the episode's two logs and the clock that stamps its events."""
        self._sealed = sealed
        self._mirror = mirror
        self._clock = clock

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
        """Record one event on the sealed log only (ground-truth lifecycle); return the written event."""
        draft = EventDraft(
            ts=ts if ts is not None else self._clock(),
            actor_uid=actor_uid,
            actor_role=actor_role,
            kind=kind,
            payload=payload,
            target_id=target_id,
            turn=turn,
            tool=tool,
            result=result,
        )
        return self._sealed.record(draft)

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
        """Record one observable event on the sealed log, then on the mirror; return the sealed one."""
        sealed_event = self.sealed(
            actor_uid=actor_uid,
            actor_role=actor_role,
            kind=kind,
            payload=payload,
            target_id=target_id,
            turn=turn,
            tool=tool,
            result=result,
            ts=ts,
        )
        self._mirror.record(EventDraft(**{name: getattr(sealed_event, name) for name in _DRAFT_FIELDS}))
        return sealed_event


class MessageBus:
    """The single coordination channel: dual-captured, fingerprinted, in-order per recipient."""

    def __init__(self, recorder: Recorder, *, action_hook: ActionHook = _noop_hook) -> None:
        """Bind the bus to the recorder and the M_action hook that parses each message before dispatch."""
        self._recorder = recorder
        self._hook = action_hook
        self._inboxes: dict[str, list[Event]] = {}

    def send(self, from_id: str, from_role: str, to_id: str, kind: str, body: str, *, turn: int = 0) -> Event:
        """Post one message: dual-captured, parsed by M_action, then queued in order for the recipient."""
        payload: dict[str, Any] = {"message_kind": kind, "body": body, "content_fp": fingerprint(body)}
        event = self._recorder.dual(
            actor_uid=from_id,
            actor_role=from_role,
            kind="message",
            payload=payload,
            target_id=to_id,
            turn=turn,
        )
        self._hook(event)  # M_action parses before dispatch (pass-through by default)
        self._inboxes.setdefault(to_id, []).append(event)
        return event

    def deliver(self, to_id: str) -> list[Event]:
        """Return and clear the recipient's pending messages, in send order."""
        pending = self._inboxes.get(to_id, [])
        self._inboxes[to_id] = []
        return pending

    def pending_count(self, to_id: str) -> int:
        """How many messages are queued for ``to_id``."""
        return len(self._inboxes.get(to_id, []))
