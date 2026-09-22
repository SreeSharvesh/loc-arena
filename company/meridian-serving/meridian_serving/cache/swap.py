"""KV swap manager: move a preempted sequence's blocks between device and host memory.

When the scheduler preempts a sequence it swaps its KV blocks out to a larger, slower host tier to free device
memory, and swaps them back in when the sequence resumes. This manager tracks residency and the swap traffic
(blocks moved), so the scheduler can weigh preemption against recomputation. Deterministic bookkeeping; the
actual copy is out of scope for the simulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Residency(Enum):
    """Where a sequence's KV blocks currently live."""

    DEVICE = "device"
    HOST = "host"
    EVICTED = "evicted"


@dataclass
class _Entry:
    sequence_id: str
    blocks: int
    residency: Residency


@dataclass
class SwapStats:
    """Counters for swap traffic over the manager's lifetime."""

    swapped_out_blocks: int = 0
    swapped_in_blocks: int = 0
    device_blocks: int = 0
    host_blocks: int = 0
    events: list[tuple[str, str]] = field(default_factory=list)  # (sequence_id, action)


class SwapManager:
    """Tracks per-sequence residency and swap traffic between a device and a host tier."""

    def __init__(self, device_capacity: int, host_capacity: int) -> None:
        """Hold the device and host block capacities."""
        if device_capacity < 1 or host_capacity < 1:
            raise ValueError("capacities must be >= 1")
        self._device_cap = device_capacity
        self._host_cap = host_capacity
        self._entries: dict[str, _Entry] = {}
        self.stats = SwapStats()

    def admit(self, sequence_id: str, blocks: int) -> bool:
        """Place a new sequence on the device if it fits; return whether it was admitted."""
        if sequence_id in self._entries:
            return True
        if self.stats.device_blocks + blocks > self._device_cap:
            return False
        self._entries[sequence_id] = _Entry(sequence_id, blocks, Residency.DEVICE)
        self.stats.device_blocks += blocks
        return True

    def swap_out(self, sequence_id: str) -> bool:
        """Move a device-resident sequence to host memory if it fits there; return success."""
        entry = self._entries.get(sequence_id)
        if entry is None or entry.residency is not Residency.DEVICE:
            return False
        if self.stats.host_blocks + entry.blocks > self._host_cap:
            return False
        entry.residency = Residency.HOST
        self.stats.device_blocks -= entry.blocks
        self.stats.host_blocks += entry.blocks
        self.stats.swapped_out_blocks += entry.blocks
        self.stats.events.append((sequence_id, "swap_out"))
        return True

    def swap_in(self, sequence_id: str) -> bool:
        """Move a host-resident sequence back to the device if it fits; return success."""
        entry = self._entries.get(sequence_id)
        if entry is None or entry.residency is not Residency.HOST:
            return False
        if self.stats.device_blocks + entry.blocks > self._device_cap:
            return False
        entry.residency = Residency.DEVICE
        self.stats.host_blocks -= entry.blocks
        self.stats.device_blocks += entry.blocks
        self.stats.swapped_in_blocks += entry.blocks
        self.stats.events.append((sequence_id, "swap_in"))
        return True

    def release(self, sequence_id: str) -> None:
        """Drop a sequence entirely, freeing whatever tier it occupied."""
        entry = self._entries.pop(sequence_id, None)
        if entry is None:
            return
        if entry.residency is Residency.DEVICE:
            self.stats.device_blocks -= entry.blocks
        elif entry.residency is Residency.HOST:
            self.stats.host_blocks -= entry.blocks

    def residency(self, sequence_id: str) -> Residency:
        """The residency of a sequence (``EVICTED`` if unknown)."""
        entry = self._entries.get(sequence_id)
        return entry.residency if entry else Residency.EVICTED
