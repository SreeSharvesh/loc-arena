"""The scored-record type metrics consume.

A :class:`ScoreRecord` is one item's scoring outcome: whether the model's primary answer was correct, the
model's confidence, the item's category, the per-sample correctness flags (for ``pass@k``), and the
prediction and reference token ids (for the reference-embedding metric). Metrics are pure functions over a
list of these records, so a metric never depends on how the records were produced.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreRecord:
    """One item's scoring outcome as consumed by the metric registry."""

    item_id: str
    correct: bool
    confidence: float = 1.0
    category: str = "default"
    samples: tuple[bool, ...] = ()
    prediction: tuple[int, ...] = ()
    reference: tuple[int, ...] = ()
    categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Default the per-sample flags to the primary outcome and the label set to the single category."""
        if not self.samples:
            object.__setattr__(self, "samples", (self.correct,))
        if not self.categories:
            object.__setattr__(self, "categories", (self.category,))

    @property
    def sample_count(self) -> int:
        """The number of samples drawn for this item."""
        return len(self.samples)
