"""A poll-based config watcher that reloads when a source changes and notifies subscribers.

Long-lived services watch their config for changes (a flag flip, a threshold retune) without a restart. A
:class:`ConfigWatcher` holds a loader callable and a content fingerprint; ``poll`` re-loads, and if the
content
changed, updates the current config and calls each subscriber with the new mapping. Polling is explicit
(driven
by the caller or a scheduler) so behavior stays deterministic and testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from meridian_common.serde.canonical import fingerprint

Reloader = Callable[[], dict[str, Any]]
Listener = Callable[[dict[str, Any]], None]


class ConfigWatcher:
    """Watches a config source via a reloader callable and notifies listeners on change."""

    def __init__(self, reloader: Reloader) -> None:
        """Load the initial config from ``reloader`` and remember its fingerprint."""
        self._reloader = reloader
        self._current = reloader()
        self._fingerprint = fingerprint(self._current)
        self._listeners: list[Listener] = []

    @property
    def current(self) -> dict[str, Any]:
        """The most recently loaded config."""
        return dict(self._current)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register ``listener`` to be called with the new config on every change; returns an unsubscribe."""
        self._listeners.append(listener)

        def _off() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _off

    def poll(self) -> bool:
        """Re-load; if content changed, update, notify listeners, and return True (else False)."""
        new = self._reloader()
        fp = fingerprint(new)
        if fp == self._fingerprint:
            return False
        self._current = new
        self._fingerprint = fp
        for listener in list(self._listeners):
            listener(dict(new))
        return True
