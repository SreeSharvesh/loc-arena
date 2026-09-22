from __future__ import annotations

import pytest

from meridian_serving.cache import KVCache, LfuPolicy, LruPolicy
from meridian_serving.cache.eviction import LruPolicy as _Lru
from meridian_serving.errors import CacheMiss


def test_put_get_value_and_served_without_eviction() -> None:
    c = KVCache(capacity=4)
    c.put("a", 10)
    c.put("b", 20)
    assert c.value("a") == 10 and c.value("b") == 20
    assert c.served("a") == 10 and c.served("b") == 20  # no eviction -> served tracks value
    assert len(c) == 2 and "a" in c


def test_update_refreshes_value_and_served() -> None:
    c = KVCache(capacity=4)
    c.put("a", 10)
    c.put("a", 99)  # in-place update (no eviction)
    assert c.value("a") == 99 and c.served("a") == 99


def test_miss_raises_and_get_default() -> None:
    c = KVCache(capacity=2)
    with pytest.raises(CacheMiss):
        c.value("missing")
    assert c.get("missing", default=-1) == -1


def test_lru_evicts_least_recently_used() -> None:
    c = KVCache(capacity=2, policy=LruPolicy())
    c.put("a", 1)
    c.put("b", 2)
    c.value("a")  # touch a -> b is now LRU
    c.put("c", 3)  # evicts b
    assert "a" in c and "c" in c and "b" not in c
    assert c.evictions == 1


def test_lfu_evicts_least_frequently_used() -> None:
    c = KVCache(capacity=2, policy=LfuPolicy())
    c.put("a", 1)
    c.put("b", 2)
    c.value("a")
    c.value("a")  # a used more
    c.put("c", 3)  # evicts b (less frequent)
    assert "a" in c and "b" not in c


def test_capacity_bound_holds() -> None:
    c = KVCache(capacity=3)
    for i in range(10):
        c.put(f"k{i}", i)
    assert len(c) == 3


def test_slot_of_and_contains() -> None:
    c = KVCache(capacity=2)
    c.put("a", 1)
    assert isinstance(c.slot_of("a"), int)
    with pytest.raises(CacheMiss):
        c.slot_of("nope")


def test_invalid_capacity() -> None:
    with pytest.raises(ValueError):
        KVCache(capacity=0)


def test_lru_policy_victim_of_empty_raises() -> None:
    with pytest.raises(KeyError):
        _Lru().victim()
