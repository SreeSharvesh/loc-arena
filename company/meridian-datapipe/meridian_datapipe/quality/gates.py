"""Quality gates: length bounds, a language heuristic, and a corpus duplicate-rate gate.

A single :class:`QualityGate` bundles the thresholds. Applied to a document it checks the text length against
the min/max bounds and the fraction of alphabetic characters against a floor (a cheap language/garbage
heuristic). Applied to a corpus it additionally checks the exact-duplicate rate against a ceiling. Every check
contributes a human-readable reason to the :class:`QualityReport` when it fails; a report with no reasons
passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian_datapipe.dedup.exact import exact_dedup
from meridian_datapipe.errors import QualityError
from meridian_datapipe.types import Document


def alpha_ratio(text: str) -> float:
    """The fraction of non-whitespace characters in ``text`` that are alphabetic (1.0 for empty text)."""
    non_space = [c for c in text if not c.isspace()]
    if not non_space:
        return 1.0
    return sum(1 for c in non_space if c.isalpha()) / len(non_space)


@dataclass(frozen=True)
class QualityReport:
    """The outcome of a quality evaluation: whether it passed, the failure reasons, and how many docs ran."""

    passed: bool
    reasons: tuple[str, ...] = ()
    checked: int = 0
    failing_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failure_count(self) -> int:
        """The number of documents that failed at least one per-document gate."""
        return len(self.failing_ids)


@dataclass(frozen=True)
class QualityGate:
    """Bundled quality thresholds applied to a document or a corpus."""

    min_length: int = 1
    max_length: int = 0
    min_alpha_ratio: float = 0.0
    max_duplicate_rate: float = 1.0

    def __post_init__(self) -> None:
        """Reject contradictory bounds (e.g. a max length below the min length)."""
        if self.max_length and self.max_length < self.min_length:
            raise QualityError("max_length is below min_length", code="datapipe.quality")
        if not 0.0 <= self.min_alpha_ratio <= 1.0:
            raise QualityError("min_alpha_ratio must be in [0, 1]", code="datapipe.quality")
        if not 0.0 <= self.max_duplicate_rate <= 1.0:
            raise QualityError("max_duplicate_rate must be in [0, 1]", code="datapipe.quality")

    def document_reasons(self, doc: Document) -> list[str]:
        """The per-document gate failures for ``doc`` (empty when it passes every per-document gate)."""
        reasons: list[str] = []
        if doc.length < self.min_length:
            reasons.append(f"length {doc.length} below min {self.min_length}")
        if self.max_length and doc.length > self.max_length:
            reasons.append(f"length {doc.length} above max {self.max_length}")
        ratio = alpha_ratio(doc.text)
        if ratio < self.min_alpha_ratio:
            reasons.append(f"alpha ratio {ratio:.3f} below min {self.min_alpha_ratio:.3f}")
        return reasons

    def check_document(self, doc: Document) -> QualityReport:
        """Evaluate a single document against the per-document gates."""
        reasons = self.document_reasons(doc)
        failing = (doc.doc_id,) if reasons else ()
        return QualityReport(passed=not reasons, reasons=tuple(reasons), checked=1, failing_ids=failing)

    def evaluate(self, docs: list[Document]) -> QualityReport:
        """Evaluate a corpus: every per-document gate plus the corpus duplicate-rate gate."""
        reasons: list[str] = []
        failing: list[str] = []
        for doc in docs:
            doc_reasons = self.document_reasons(doc)
            if doc_reasons:
                failing.append(doc.doc_id)
                reasons.extend(f"{doc.doc_id}: {reason}" for reason in doc_reasons)
        if docs:
            duplicate_rate = exact_dedup(docs).duplicate_rate()
            if duplicate_rate > self.max_duplicate_rate:
                reasons.append(f"duplicate rate {duplicate_rate:.3f} above max {self.max_duplicate_rate:.3f}")
        return QualityReport(
            passed=not reasons,
            reasons=tuple(reasons),
            checked=len(docs),
            failing_ids=tuple(failing),
        )
