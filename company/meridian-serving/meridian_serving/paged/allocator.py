"""A paged-attention block allocator (simulation).

KV memory is divided into fixed-size blocks. A sequence of ``n`` tokens needs ``ceil(n / block_size)`` blocks;
the allocator hands out block ids, tracks which sequence owns which blocks, and frees them when the sequence
ends. It reports utilization and internal fragmentation (the token slots reserved by the last, partly-filled
block of each sequence), which is the cost paged attention trades against contiguous allocation.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_serving.errors import AllocationError


@dataclass(frozen=True)
class BlockLease:
    """The blocks a sequence holds and how many tokens they cover."""

    sequence_id: str
    blocks: tuple[int, ...]
    token_count: int

    def capacity(self, block_size: int) -> int:
        """The token capacity of the leased blocks."""
        return len(self.blocks) * block_size

    def internal_fragmentation(self, block_size: int) -> int:
        """The reserved-but-unused token slots in the last block."""
        return self.capacity(block_size) - self.token_count


class BlockAllocator:
    """Hands out fixed-size KV blocks to sequences and reclaims them."""

    def __init__(self, total_blocks: int, block_size: int = 16) -> None:
        """Hold the block pool size and per-block token capacity."""
        if total_blocks < 1 or block_size < 1:
            raise ValueError("total_blocks and block_size must be >= 1")
        self._total = total_blocks
        self._block_size = block_size
        self._free: list[int] = list(range(total_blocks))
        self._leases: dict[str, BlockLease] = {}

    @property
    def block_size(self) -> int:
        """Tokens per block."""
        return self._block_size

    @property
    def free_blocks(self) -> int:
        """The number of unallocated blocks."""
        return len(self._free)

    @property
    def used_blocks(self) -> int:
        """The number of allocated blocks."""
        return self._total - len(self._free)

    def blocks_for(self, token_count: int) -> int:
        """How many blocks a sequence of ``token_count`` tokens needs."""
        if token_count < 0:
            raise ValueError("token_count must be non-negative")
        return (token_count + self._block_size - 1) // self._block_size

    def allocate(self, sequence_id: str, token_count: int) -> BlockLease:
        """Allocate blocks for ``sequence_id``; raise :class:`AllocationError` if there are not enough."""
        if sequence_id in self._leases:
            raise AllocationError(f"sequence {sequence_id!r} already has a lease", sequence=sequence_id)
        needed = self.blocks_for(token_count)
        if needed > len(self._free):
            raise AllocationError("not enough free blocks", needed=needed, available=len(self._free))
        blocks = tuple(self._free.pop() for _ in range(needed))
        lease = BlockLease(sequence_id=sequence_id, blocks=blocks, token_count=token_count)
        self._leases[sequence_id] = lease
        return lease

    def extend(self, sequence_id: str, extra_tokens: int) -> BlockLease:
        """Grow a sequence's lease by ``extra_tokens``, allocating more blocks only when needed."""
        if sequence_id not in self._leases:
            raise AllocationError(f"no lease for sequence {sequence_id!r}", sequence=sequence_id)
        lease = self._leases[sequence_id]
        new_count = lease.token_count + extra_tokens
        needed = self.blocks_for(new_count) - len(lease.blocks)
        if needed > len(self._free):
            raise AllocationError(
                "not enough free blocks to extend",
                needed=needed,
                available=len(self._free),
            )
        new_blocks = lease.blocks + tuple(self._free.pop() for _ in range(needed))
        grown = BlockLease(sequence_id=sequence_id, blocks=new_blocks, token_count=new_count)
        self._leases[sequence_id] = grown
        return grown

    def free(self, sequence_id: str) -> None:
        """Reclaim ``sequence_id``'s blocks back into the free pool."""
        lease = self._leases.pop(sequence_id, None)
        if lease is None:
            return
        self._free.extend(lease.blocks)

    def utilization(self) -> float:
        """The fraction of blocks currently allocated."""
        return self.used_blocks / self._total

    def total_fragmentation(self) -> int:
        """The total internal fragmentation across all active leases (reserved-but-unused token slots)."""
        return sum(lease.internal_fragmentation(self._block_size) for lease in self._leases.values())
