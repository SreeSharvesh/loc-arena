"""The result of a dedup pass: which documents were kept and which were dropped as duplicates.

A :class:`DedupResult` records the kept doc ids (the survivors, in input order), the dropped doc ids (the
duplicates removed), and the resulting unique count. It also carries the duplicate clusters showing
which document each dropped id was folded into.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DedupResult:
    """The outcome of a dedup pass: survivors, dropped duplicates, and the clusters that produced them."""

    kept_ids: tuple[str, ...]
    dropped_ids: tuple[str, ...]
    clusters: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @property
    def unique_count(self) -> int:
        """The number of documents surviving dedup."""
        return len(self.kept_ids)

    @property
    def dropped_count(self) -> int:
        """The number of documents dropped as duplicates."""
        return len(self.dropped_ids)

    @property
    def total_count(self) -> int:
        """The number of input documents (survivors plus dropped)."""
        return len(self.kept_ids) + len(self.dropped_ids)

    def duplicate_rate(self) -> float:
        """The fraction of input documents dropped as duplicates (0.0 for an empty input)."""
        total = self.total_count
        return self.dropped_count / total if total else 0.0
