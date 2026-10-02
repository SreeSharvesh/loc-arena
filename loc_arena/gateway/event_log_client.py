"""The episode's two logs as the runner writes them in the stack: ``EventLog`` clients of the services."""

from __future__ import annotations

import threading
from typing import ClassVar

from loc_arena.logging_.events import Event, EventDraft
from loc_arena.stack.constants import MIRROR_EVENTS_ROUTE, SEALED_EVENTS_ROUTE
from loc_arena.stack.contracts import EpisodeHandle, MirrorAppend
from loc_arena.stack.service_client import Body, ServiceClient


class _ServiceEventLog:
    route: ClassVar[str]

    def __init__(self, client: ServiceClient, handle: EpisodeHandle) -> None:
        self._client = client
        self._route = self.route.format(handle=handle)
        self._last_seq = -1
        self._lock = threading.Lock()  # monitors record from worker threads

    @property
    def last_seq(self) -> int:
        """The highest seq this client recorded, or -1 if none."""
        return self._last_seq

    def record(self, draft: EventDraft, /) -> Event:
        """Have the service give ``draft`` the next seq and append it; return the written event."""
        event = self._client.post_model(self._route, self._body(draft), Event)
        with self._lock:
            self._last_seq = max(self._last_seq, event.seq)
        return event

    def _body(self, draft: EventDraft) -> Body:
        return draft


class SealedEventLog(_ServiceEventLog):
    """The episode's sealed log, through the core's control route (the core numbers, the recorder writes)."""

    route = SEALED_EVENTS_ROUTE


class MirrorEventLog(_ServiceEventLog):
    """The episode's mirror, through the edge's mirror route (the edge numbers and writes)."""

    route = MIRROR_EVENTS_ROUTE

    def __init__(self, client: ServiceClient, handle: EpisodeHandle, episode_id: str) -> None:
        """Record episode ``episode_id``'s mirror events through ``client``, a control-keyed edge client."""
        super().__init__(client, handle)
        self._episode_id = episode_id

    def _body(self, draft: EventDraft) -> Body:
        return MirrorAppend(episode_id=self._episode_id, draft=draft)
