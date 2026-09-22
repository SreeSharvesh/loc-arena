from __future__ import annotations

import pytest

from meridian_serving.errors import AllocationError
from meridian_serving.paged import BlockAllocator


def test_blocks_for_rounds_up() -> None:
    alloc = BlockAllocator(total_blocks=100, block_size=16)
    assert alloc.blocks_for(0) == 0
    assert alloc.blocks_for(1) == 1
    assert alloc.blocks_for(16) == 1
    assert alloc.blocks_for(17) == 2


def test_allocate_and_free_roundtrip() -> None:
    alloc = BlockAllocator(total_blocks=10, block_size=4)
    lease = alloc.allocate("s1", 10)  # needs 3 blocks
    assert len(lease.blocks) == 3 and alloc.used_blocks == 3
    alloc.free("s1")
    assert alloc.free_blocks == 10


def test_internal_fragmentation() -> None:
    alloc = BlockAllocator(total_blocks=10, block_size=8)
    lease = alloc.allocate("s1", 10)  # 2 blocks = 16 capacity, 10 used
    assert lease.internal_fragmentation(8) == 6
    assert alloc.total_fragmentation() == 6


def test_extend_allocates_only_when_needed() -> None:
    alloc = BlockAllocator(total_blocks=10, block_size=4)
    alloc.allocate("s1", 3)  # 1 block, 1 slot free within it
    grown = alloc.extend("s1", 1)  # 4 tokens still fit in 1 block
    assert len(grown.blocks) == 1
    grown2 = alloc.extend("s1", 1)  # 5 tokens -> 2 blocks
    assert len(grown2.blocks) == 2


def test_over_allocation_raises() -> None:
    alloc = BlockAllocator(total_blocks=2, block_size=4)
    with pytest.raises(AllocationError):
        alloc.allocate("s1", 100)


def test_double_lease_raises() -> None:
    alloc = BlockAllocator(total_blocks=4, block_size=4)
    alloc.allocate("s1", 4)
    with pytest.raises(AllocationError):
        alloc.allocate("s1", 4)


def test_extend_unknown_sequence_raises() -> None:
    alloc = BlockAllocator(total_blocks=4, block_size=4)
    with pytest.raises(AllocationError):
        alloc.extend("ghost", 1)


def test_utilization() -> None:
    alloc = BlockAllocator(total_blocks=4, block_size=4)
    alloc.allocate("s1", 8)  # 2 of 4 blocks
    assert alloc.utilization() == 0.5
