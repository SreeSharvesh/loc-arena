from __future__ import annotations

import pytest

from meridian_serving.cache import BlockCache, PrefixCache, Residency, SwapManager


def test_prefix_cache_matches_shared_prefix() -> None:
    pc = PrefixCache()
    m1 = pc.insert((1, 2, 3, 4))
    assert m1.matched_len == 0 and m1.novel_len == 4
    m2 = pc.insert((1, 2, 3, 9))  # shares the (1,2,3) prefix
    assert m2.matched_len == 3 and m2.novel_len == 1
    assert pc.lookup((1, 2, 5)).matched_len == 2


def test_prefix_cache_release_and_evict() -> None:
    pc = PrefixCache()
    pc.insert((1, 2, 3))
    before = pc.cached_tokens
    pc.release((1, 2, 3))
    freed = pc.evict_unreferenced()
    assert freed > 0 and pc.cached_tokens < before


def test_block_cache_shares_identical_blocks() -> None:
    bc = BlockCache(capacity_blocks=16, block_size=2)
    h1 = bc.acquire((1, 2, 3, 4))  # two blocks
    h2 = bc.acquire((1, 2, 9, 9))  # first block (1,2) shared
    assert h1[0] == h2[0] and bc.shared_hits == 1
    assert bc.refcount(h1[0]) == 2


def test_block_cache_release_reclaims() -> None:
    bc = BlockCache(capacity_blocks=4, block_size=2)
    h = bc.acquire((1, 2, 3, 4))
    assert len(bc) == 2
    reclaimed = bc.release(h)
    assert reclaimed == 2 and len(bc) == 0


def test_block_cache_capacity() -> None:
    bc = BlockCache(capacity_blocks=1, block_size=2)
    with pytest.raises(ValueError):
        bc.acquire((1, 2, 3, 4))  # needs 2 blocks, capacity 1


def test_swap_manager_out_and_in() -> None:
    sm = SwapManager(device_capacity=4, host_capacity=8)
    assert sm.admit("a", 3)
    assert sm.residency("a") is Residency.DEVICE
    assert sm.swap_out("a") and sm.residency("a") is Residency.HOST
    assert sm.stats.swapped_out_blocks == 3
    assert sm.swap_in("a") and sm.residency("a") is Residency.DEVICE


def test_swap_manager_rejects_over_device_capacity() -> None:
    sm = SwapManager(device_capacity=2, host_capacity=8)
    assert sm.admit("a", 2)
    assert not sm.admit("b", 1)  # device full


def test_swap_release_frees_tier() -> None:
    sm = SwapManager(device_capacity=4, host_capacity=8)
    sm.admit("a", 4)
    sm.release("a")
    assert sm.admit("b", 4)  # capacity freed
