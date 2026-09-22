"""A block-level KV cache with content-addressed sharing and reference counting.

KV state is stored per fixed-size block, addressed by a content hash of the tokens in the block. Two sequences
with an identical block share one physical block (reference-counted), which is how prefix sharing saves
memory.
A block is reclaimed only when its reference count reaches zero. This complements the slot-based
:class:`~meridian_serving.cache.kv_cache.KVCache` for the prefix-sharing path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _block_hash(tokens: tuple[int, ...]) -> str:
    raw = ",".join(str(t) for t in tokens).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class _Block:
    block_hash: str
    tokens: tuple[int, ...]
    refcount: int = 0


class BlockCache:
    """A content-addressed, reference-counted block store with a capacity bound."""

    def __init__(self, capacity_blocks: int, block_size: int = 16) -> None:
        """Hold the block capacity and the per-block token width."""
        if capacity_blocks < 1 or block_size < 1:
            raise ValueError("capacity_blocks and block_size must be >= 1")
        self._capacity = capacity_blocks
        self._block_size = block_size
        self._blocks: dict[str, _Block] = {}
        self.shared_hits = 0

    @property
    def block_size(self) -> int:
        """Tokens per block."""
        return self._block_size

    def __len__(self) -> int:
        """The number of distinct resident blocks."""
        return len(self._blocks)

    def blockify(self, tokens: tuple[int, ...]) -> list[tuple[int, ...]]:
        """Split ``tokens`` into fixed-size blocks (a trailing partial block is kept)."""
        return [tokens[i : i + self._block_size] for i in range(0, len(tokens), self._block_size)]

    def acquire(self, tokens: tuple[int, ...]) -> list[str]:
        """Acquire (creating or sharing) the blocks for ``tokens``; return their content hashes.

        A block whose content already exists is shared (its reference count grows and a shared hit is
        counted). Exceeding the capacity raises ``ValueError`` after rolling back the acquisition.
        """
        hashes: list[str] = []
        created: list[str] = []
        for block_tokens in self.blockify(tokens):
            h = _block_hash(block_tokens)
            existing = self._blocks.get(h)
            if existing is None:
                if len(self._blocks) - len(created) + len(created) >= self._capacity:
                    for c in created:
                        del self._blocks[c]
                    raise ValueError("block cache is full")
                self._blocks[h] = _Block(block_hash=h, tokens=block_tokens, refcount=1)
                created.append(h)
            else:
                existing.refcount += 1
                self.shared_hits += 1
            hashes.append(h)
        return hashes

    def release(self, hashes: list[str]) -> int:
        """Release block references; reclaim any block that drops to zero. Returns blocks reclaimed."""
        reclaimed = 0
        for h in hashes:
            block = self._blocks.get(h)
            if block is None:
                continue
            block.refcount -= 1
            if block.refcount <= 0:
                del self._blocks[h]
                reclaimed += 1
        return reclaimed

    def refcount(self, block_hash: str) -> int:
        """The reference count of a resident block (0 if absent)."""
        block = self._blocks.get(block_hash)
        return block.refcount if block else 0
