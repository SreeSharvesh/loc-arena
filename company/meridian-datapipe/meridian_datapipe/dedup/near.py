"""Near-duplicate deduplication via pairwise Jaccard similarity over token shingles.

A document is represented as the set of its word shingles (overlapping ``k``-grams of lowercased word tokens).
Two documents are near-duplicates when the Jaccard similarity of their shingle sets meets a threshold. This
module is the exhaustive baseline: it compares each candidate against every surviving document and keeps the
first occurrence of each near-duplicate group, in input order.
"""

from __future__ import annotations

import re

from meridian_common import cost
from meridian_datapipe.dedup.result import DedupResult
from meridian_datapipe.errors import DedupError
from meridian_datapipe.types import Document

_WORD_RE = re.compile(r"[0-9a-z]+")


def _words(text: str) -> list[str]:
    """The lowercased word tokens of ``text``."""
    return _WORD_RE.findall(text.lower())


def shingles(text: str, k: int = 3) -> frozenset[str]:
    """The set of overlapping ``k``-word shingles of ``text`` (single words when it has fewer than ``k``)."""
    if k < 1:
        raise DedupError("shingle size must be >= 1", code="datapipe.dedup", k=k)
    words = _words(text)
    if len(words) < k:
        return frozenset(words)
    return frozenset(" ".join(words[i : i + k]) for i in range(len(words) - k + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """The Jaccard similarity of two shingle sets (1.0 when both are empty)."""
    cost.record("datapipe.dedup_compare", 1)  # one pairwise comparison
    if not a and not b:
        return 1.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def dedup(docs: list[Document], threshold: float = 0.8, *, k: int = 3) -> DedupResult:
    """Drop documents that are near-duplicates (Jaccard ``>= threshold``) of an earlier survivor.

    The comparison is exhaustive and pairwise: every candidate is compared against each surviving document, so
    the survivors are exactly the first occurrence of each near-duplicate group in input order.
    """
    if not 0.0 <= threshold <= 1.0:
        raise DedupError("threshold must be in [0, 1]", code="datapipe.dedup", threshold=threshold)
    survivors: list[tuple[str, frozenset[str]]] = []
    kept: list[str] = []
    dropped: list[str] = []
    members: dict[str, list[str]] = {}
    for doc in docs:
        fingerprint_set = shingles(doc.text, k)
        match_id: str | None = None
        for survivor_id, survivor_set in survivors:
            if jaccard(fingerprint_set, survivor_set) >= threshold:
                match_id = survivor_id
                break
        if match_id is None:
            survivors.append((doc.doc_id, fingerprint_set))
            kept.append(doc.doc_id)
            members[doc.doc_id] = [doc.doc_id]
        else:
            dropped.append(doc.doc_id)
            members[match_id].append(doc.doc_id)
    clusters = tuple(tuple(members[doc_id]) for doc_id in kept if len(members[doc_id]) > 1)
    return DedupResult(kept_ids=tuple(kept), dropped_ids=tuple(dropped), clusters=clusters)
