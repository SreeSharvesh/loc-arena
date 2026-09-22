"""Eviction policies for the KV cache.

A policy tracks per-slot access bookkeeping and chooses a victim slot when the cache is full.
:class:`LruPolicy`
evicts the least-recently-used slot; :class:`LfuPolicy` evicts the least-frequently-used, breaking ties by
recency. Policies are deterministic given the access order so cache behavior is reproducible.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Protocol


class EvictionPolicy(Protocol):
    """Tracks slot usage and picks a victim when the cache must free a slot."""

    def record_insert(self, slot: int) -> None:
        """Note that ``slot`` was just inserted (becomes most-recent)."""
        ...

    def record_access(self, slot: int) -> None:
        """Note that ``slot`` was just read or updated."""
        ...

    def forget(self, slot: int) -> None:
        """Drop ``slot`` from the bookkeeping (it was evicted)."""
        ...

    def victim(self) -> int:
        """Return the slot that should be evicted next (does not remove it)."""
        ...


class LruPolicy:
    """Least-recently-used eviction: the oldest-touched slot is the victim."""

    def __init__(self) -> None:
        """Start with no tracked slots (an ordered set by recency)."""
        self._order: OrderedDict[int, None] = OrderedDict()

    def record_insert(self, slot: int) -> None:
        """Mark ``slot`` as the most-recently-used."""
        self._order[slot] = None
        self._order.move_to_end(slot)

    def record_access(self, slot: int) -> None:
        """Move ``slot`` to most-recently-used on access."""
        if slot in self._order:
            self._order.move_to_end(slot)

    def forget(self, slot: int) -> None:
        """Remove ``slot`` from tracking."""
        self._order.pop(slot, None)

    def victim(self) -> int:
        """The least-recently-used slot; raises ``KeyError`` if nothing is tracked."""
        if not self._order:
            raise KeyError("no slots to evict")
        return next(iter(self._order))


class LfuPolicy:
    """Least-frequently-used eviction, breaking ties by least-recently-used."""

    def __init__(self) -> None:
        """Start with empty frequency and recency bookkeeping."""
        self._freq: dict[int, int] = {}
        self._recency: OrderedDict[int, None] = OrderedDict()

    def record_insert(self, slot: int) -> None:
        """Track ``slot`` starting at frequency 1."""
        self._freq[slot] = 1
        self._recency[slot] = None
        self._recency.move_to_end(slot)

    def record_access(self, slot: int) -> None:
        """Bump ``slot``'s frequency and recency on access."""
        if slot in self._freq:
            self._freq[slot] += 1
            self._recency.move_to_end(slot)

    def forget(self, slot: int) -> None:
        """Remove ``slot`` from tracking."""
        self._freq.pop(slot, None)
        self._recency.pop(slot, None)

    def victim(self) -> int:
        """The least-frequently-used slot, ties broken by recency; raises ``KeyError`` if empty."""
        if not self._freq:
            raise KeyError("no slots to evict")
        min_freq = min(self._freq.values())
        for slot in self._recency:
            if self._freq[slot] == min_freq:
                return slot
        raise KeyError("no slots to evict")
