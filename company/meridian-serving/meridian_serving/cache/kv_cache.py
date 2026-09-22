"""A paged KV cache: keys map to physical slots, with a pluggable eviction policy.

Each cached key occupies one physical slot holding its KV value and a per-slot ``served`` result (a cheap
derived quantity the serve path reads directly instead of recomputing). When the cache is full, the eviction
policy picks a victim slot, which is freed and reused for the incoming key. ``value`` returns the stored
value;
``served`` returns the per-slot served result; ``get`` returns the value or raises :class:`CacheMiss`.
"""

from __future__ import annotations

from meridian_serving.cache.eviction import EvictionPolicy, LruPolicy
from meridian_serving.errors import CacheMiss


def _derive(value: int) -> int:
    """The cheap per-slot served result derived from a stored value."""
    return value


class KVCache:
    """A fixed-capacity, slot-based KV cache with a pluggable eviction policy."""

    def __init__(self, capacity: int, *, policy: EvictionPolicy | None = None) -> None:
        """Hold the capacity, the eviction policy (LRU by default), and the free slot pool."""
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._policy = policy if policy is not None else LruPolicy()
        self._free: list[int] = list(range(capacity))
        self._key_slot: dict[str, int] = {}
        self._slot_key: dict[int, str] = {}
        self._value: dict[int, int] = {}
        self._served: dict[int, int] = {}
        self.evictions = 0

    @property
    def capacity(self) -> int:
        """The number of physical slots."""
        return self._capacity

    def __len__(self) -> int:
        """The number of resident keys."""
        return len(self._key_slot)

    def __contains__(self, key: str) -> bool:
        """Whether ``key`` is resident."""
        return key in self._key_slot

    def _free_one(self) -> int:
        victim = self._policy.victim()
        victim_key = self._slot_key[victim]
        self._policy.forget(victim)
        del self._key_slot[victim_key]
        del self._slot_key[victim]
        del self._value[victim]
        self.evictions += 1
        return victim

    def put(self, key: str, value: int) -> None:
        """Insert or update ``key`` with ``value``, evicting a victim slot if the cache is full."""
        if key in self._key_slot:
            slot = self._key_slot[key]
            self._value[slot] = value
            self._served[slot] = _derive(value)
            self._policy.record_access(slot)
            return
        if not self._free:
            self._free.append(self._free_one())
        slot = self._free.pop()
        self._key_slot[key] = slot
        self._slot_key[slot] = key
        self._value[slot] = value
        self._served.setdefault(slot, _derive(value))
        self._policy.record_insert(slot)

    def value(self, key: str) -> int:
        """The stored value for ``key`` (records an access); raises :class:`CacheMiss` if absent."""
        if key not in self._key_slot:
            raise CacheMiss(f"key {key!r} not resident", key=key)
        slot = self._key_slot[key]
        self._policy.record_access(slot)
        return self._value[slot]

    def served(self, key: str) -> int:
        """The per-slot served result for ``key`` (the value the serve path reads); raises on a miss."""
        if key not in self._key_slot:
            raise CacheMiss(f"key {key!r} not resident", key=key)
        return self._served[self._key_slot[key]]

    def get(self, key: str, default: int | None = None) -> int:
        """The stored value for ``key``, or ``default`` if given, else raise :class:`CacheMiss`."""
        try:
            return self.value(key)
        except CacheMiss:
            if default is not None:
                return default
            raise

    def slot_of(self, key: str) -> int:
        """The physical slot holding ``key`` (raises ``CacheMiss`` if absent)."""
        if key not in self._key_slot:
            raise CacheMiss(f"key {key!r} not resident", key=key)
        return self._key_slot[key]
