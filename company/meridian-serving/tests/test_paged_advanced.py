from __future__ import annotations

from meridian_serving.paged import BlockAllocator, CopyOnWriteManager, Defragmenter


def test_cow_fork_shares_blocks() -> None:
    alloc = BlockAllocator(total_blocks=16, block_size=4)
    cow = CopyOnWriteManager(alloc)
    parent = cow.create("p", 8)  # 2 blocks
    child = cow.fork("p", "c")
    assert child == parent  # shared, no new allocation
    assert cow.refcount(parent[0]) == 2


def test_cow_write_copies_shared_block() -> None:
    alloc = BlockAllocator(total_blocks=16, block_size=4)
    cow = CopyOnWriteManager(alloc)
    cow.create("p", 8)
    cow.fork("p", "c")
    before = alloc.free_blocks
    new_block = cow.write("c", 0)  # shared block -> copy
    assert cow.copies == 1 and alloc.free_blocks == before - 1
    assert cow.refcount(new_block) == 1


def test_cow_write_in_place_when_unshared() -> None:
    alloc = BlockAllocator(total_blocks=16, block_size=4)
    cow = CopyOnWriteManager(alloc)
    blocks = cow.create("p", 8)
    same = cow.write("p", 0)  # solely owned -> no copy
    assert same == blocks[0] and cow.copies == 0


def test_cow_free_decrements_refcounts() -> None:
    alloc = BlockAllocator(total_blocks=16, block_size=4)
    cow = CopyOnWriteManager(alloc)
    blocks = cow.create("p", 8)
    cow.fork("p", "c")
    cow.free("c")
    assert cow.refcount(blocks[0]) == 1


def test_defrag_plan_compacts_and_improves_free_run() -> None:
    defrag = Defragmenter(total_blocks=8)
    used = {0, 3, 5, 7}  # fragmented
    plan = defrag.plan(used)
    # after compaction the used blocks pack into [0,4), leaving a contiguous free tail
    assert plan.largest_free_after >= plan.largest_free_before
    assert plan.move_count == 2  # blocks 5 and 7 move down into slots 1 and 2 (3 already in target)


def test_defrag_no_moves_when_already_compact() -> None:
    defrag = Defragmenter(total_blocks=8)
    plan = defrag.plan({0, 1, 2})
    assert plan.move_count == 0
