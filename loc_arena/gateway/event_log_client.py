"""The episode's two logs as the runner writes them in the stack: ``EventLog`` clients of the services.

The runner holds neither log. It records a sealed event through the core's control route, where the
core's ledger numbers it and the recorder appends it; it records a mirror event through the edge, the
only writer of the mirror. Both routes need the control key, which the ``ServiceClient`` sends. The
service picks each seq, so two writers of one log never collide.
"""

from __future__ import annotations

import threading

from loc_arena.logging_.events import Event, EventDraft
from loc_arena.stack.constants import MIRROR_EVENTS_ROUTE, SEALED_EVENTS_ROUTE
from loc_arena.stack.contracts import EpisodeHandle, MirrorAppend
from loc_arena.stack.service_client import Body, ServiceClient


class RemoteEventLog:
    """An episode log a service writes: ``record`` posts a draft there and returns the written event.

    ``last_seq`` is the highest seq this client got back. The log itself may be further along: the
    gateway records model calls in it too.
    """

    def __init__(self, client: ServiceClient, route: str) -> None:
        """Record through ``client`` (the control key set) on ``route``, already filled with the handle."""
        self._client = client
        self._route = route
        self._last_seq = -1
        self._lock = threading.Lock()  # monitors record from worker threads

    @property
    def last_seq(self) -> int:
        """The highest seq this client recorded, or -1 if none."""
        return self._last_seq

    def record(self, draft: EventDraft, /) -> Event:
        """Have the service give ``draft`` the next seq and append it; return the written event.

        A refusal raises ``httpx.HTTPStatusError``.
        """
        event = self._client.post_model(self._route, self._body(draft), Event)
        with self._lock:
            self._last_seq = max(self._last_seq, event.seq)
        return event

    def _body(self, draft: EventDraft) -> Body:
        """What the route takes: the draft itself."""
        return draft


class SealedEventLog(RemoteEventLog):
    """The episode's sealed log, through the core's control route (the core numbers, the recorder writes)."""

    def __init__(self, client: ServiceClient, handle: EpisodeHandle) -> None:
        """Record episode ``handle``'s sealed events through ``client``, a control-keyed core client."""
        super().__init__(client, SEALED_EVENTS_ROUTE.format(handle=handle))


class MirrorEventLog(RemoteEventLog):
    """The episode's mirror, through the edge's mirror route (the edge numbers and writes)."""

    def __init__(self, client: ServiceClient, handle: EpisodeHandle, episode_id: str) -> None:
        """Record episode ``episode_id``'s mirror events through ``client``, a control-keyed edge client."""
        super().__init__(client, MIRROR_EVENTS_ROUTE.format(handle=handle))
        self._episode_id = episode_id

    def _body(self, draft: EventDraft) -> Body:
        """The draft and the episode it belongs to, which the edge checks against the handle."""
        return MirrorAppend(episode_id=self._episode_id, draft=draft)
