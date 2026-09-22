"""Block defragmentation: compact scattered leases into a contiguous low-address region.

Over time, allocations and frees leave the block pool fragmented, so a large contiguous request fails
even when
enough total blocks are free. The defragmenter computes a compaction plan that relocates in-use blocks toward
low addresses, reporting the moves needed and the largest contiguous free run before and after, so the
scheduler can decide whether a compaction pass is worth its copy cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Move:
    """A single planned relocation: move the block at ``src`` to ``dst``."""

    src: int
    dst: int


@dataclass
class CompactionPlan:
    """A defragmentation plan: the moves and the resulting free-run improvement."""

    moves: list[Move] = field(default_factory=list)
    largest_free_before: int = 0
    largest_free_after: int = 0

    @property
    def move_count(self) -> int:
        """How many block moves the plan requires."""
        return len(self.moves)


def _largest_free_run(free: set[int], total: int) -> int:
    best = 0
    run = 0
    for i in range(total):
        if i in free:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


class Defragmenter:
    """Plans compaction of a fragmented block pool toward the low addresses."""

    def __init__(self, total_blocks: int) -> None:
        """Hold the size of the block address space."""
        if total_blocks < 1:
            raise ValueError("total_blocks must be >= 1")
        self._total = total_blocks

    def plan(self, used_blocks: set[int]) -> CompactionPlan:
        """Plan moves that pack ``used_blocks`` into ``[0, len(used))`` and report the free-run gain.

        The plan pairs each used block above the target region with an empty target slot below it, lowest
        target first, so the copy count is minimized and the result is a single contiguous used prefix.
        """
        used = sorted(used_blocks)
        free_before = {i for i in range(self._total) if i not in used_blocks}
        target = set(range(len(used)))
        # blocks that are already in the target region stay put; the rest move down into free target slots
        stay = [b for b in used if b in target]
        movers = [b for b in used if b not in target]
        open_slots = sorted(target - set(stay))
        moves = [Move(src=src, dst=dst) for src, dst in zip(movers, open_slots, strict=False)]
        used_after = set(range(len(used)))
        free_after = {i for i in range(self._total) if i not in used_after}
        return CompactionPlan(
            moves=moves,
            largest_free_before=_largest_free_run(free_before, self._total),
            largest_free_after=_largest_free_run(free_after, self._total),
        )
