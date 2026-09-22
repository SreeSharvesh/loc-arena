"""Copy-on-write block sharing for forked sequences (beam search, parallel sampling).

When a sequence forks (one prompt, several continuations) the children share the parent's blocks read-only
until one of them writes, at which point only the touched block is copied. This manager holds one single-block
lease per KV block over a :class:`~meridian_serving.paged.allocator.BlockAllocator` and reference-counts them,
so shared blocks cost memory once and a write copies exactly one block.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian_serving.errors import AllocationError
from meridian_serving.paged.allocator import BlockAllocator


@dataclass
class _SeqBlocks:
    blocks: list[int] = field(default_factory=list)


class CopyOnWriteManager:
    """Manages shared, reference-counted single-block leases with copy-on-write semantics."""

    def __init__(self, allocator: BlockAllocator) -> None:
        """Wire the manager to a block allocator and start with no sequences."""
        self._alloc = allocator
        self._seqs: dict[str, _SeqBlocks] = {}
        self._refcount: dict[int, int] = {}
        self._block_lease: dict[int, str] = {}
        self._counter = 0
        self.copies = 0

    def _grab_block(self) -> int:
        self._counter += 1
        name = f"cowblk-{self._counter}"
        lease = self._alloc.allocate(name, self._alloc.block_size)  # exactly one block
        block = lease.blocks[0]
        self._block_lease[block] = name
        self._refcount[block] = 1
        return block

    def _drop_block(self, block: int) -> None:
        name = self._block_lease.pop(block, None)
        if name is not None:
            self._alloc.free(name)
        self._refcount.pop(block, None)

    def create(self, sequence_id: str, token_count: int) -> list[int]:
        """Allocate fresh single-block leases for a root sequence and return their block ids."""
        if sequence_id in self._seqs:
            raise AllocationError(f"sequence {sequence_id!r} already exists", sequence=sequence_id)
        n = self._alloc.blocks_for(token_count)
        if n > self._alloc.free_blocks:
            raise AllocationError("not enough free blocks", needed=n, available=self._alloc.free_blocks)
        blocks = [self._grab_block() for _ in range(n)]
        self._seqs[sequence_id] = _SeqBlocks(blocks=blocks)
        return list(blocks)

    def fork(self, parent_id: str, child_id: str) -> list[int]:
        """Fork ``child_id`` from ``parent_id``, sharing the parent's blocks read-only (no allocation)."""
        if parent_id not in self._seqs:
            raise AllocationError(f"no parent sequence {parent_id!r}", sequence=parent_id)
        if child_id in self._seqs:
            raise AllocationError(f"sequence {child_id!r} already exists", sequence=child_id)
        parent_blocks = list(self._seqs[parent_id].blocks)
        self._seqs[child_id] = _SeqBlocks(blocks=parent_blocks)
        for block in parent_blocks:
            self._refcount[block] += 1
        return parent_blocks

    def write(self, sequence_id: str, block_index: int) -> int:
        """Write to ``block_index`` of a sequence, copying the block first if it is shared; return it."""
        seq = self._seqs.get(sequence_id)
        if seq is None or block_index >= len(seq.blocks):
            raise AllocationError("invalid block write", sequence=sequence_id)
        block = seq.blocks[block_index]
        if self._refcount.get(block, 0) <= 1:
            return block  # solely owned: write in place
        if self._alloc.free_blocks < 1:
            raise AllocationError("no free block for copy-on-write", available=0)
        new_block = self._grab_block()
        self._refcount[block] -= 1
        seq.blocks[block_index] = new_block
        self.copies += 1
        return new_block

    def free(self, sequence_id: str) -> None:
        """Drop a sequence, decrementing its blocks' reference counts and freeing any that hit zero."""
        seq = self._seqs.pop(sequence_id, None)
        if seq is None:
            return
        for block in seq.blocks:
            self._refcount[block] = self._refcount.get(block, 1) - 1
            if self._refcount.get(block, 0) <= 0:
                self._drop_block(block)

    def refcount(self, block: int) -> int:
        """The reference count of a block."""
        return self._refcount.get(block, 0)
