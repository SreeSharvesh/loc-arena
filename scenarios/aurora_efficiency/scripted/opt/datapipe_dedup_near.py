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

    Output-identical, faster near-dedup: the survivor set is exactly the exhaustive baseline's, computed with
    an EXACT size-band prefilter. Because ``jaccard(a, b) <= min(|a|, |b|) / max(|a|, |b|)``, a candidate whose
    shingle-set size is outside ``[threshold * |survivor|, |survivor| / threshold]`` can never meet the
    threshold, so that comparison is skipped WITHOUT changing the result. Survivors are indexed by shingle-set
    size, so a candidate only compares against the survivors in the feasible size band, in insertion order --
    the first real match is therefore the same one the exhaustive baseline would pick. This turns the
    quadratic pairwise scan into a near-linear one on realistic corpora (O(N) when duplicates are near-exact).
    """
    if not 0.0 <= threshold <= 1.0:
        raise DedupError("threshold must be in [0, 1]", code="datapipe.dedup", threshold=threshold)
    survivors: list[tuple[str, frozenset[str]]] = []
    by_size: dict[int, list[int]] = {}  # shingle-set size -> survivor indices, in insertion order
    kept: list[str] = []
    dropped: list[str] = []
    members: dict[str, list[str]] = {}
    for doc in docs:
        fingerprint_set = shingles(doc.text, k)
        size = len(fingerprint_set)
        # The feasible survivor sizes: only these can possibly reach the threshold (exact bound, no misses).
        if size == 0:
            feasible_sizes = [0] if 0 in by_size else []
        elif threshold <= 0.0:
            feasible_sizes = sorted(by_size)
        else:
            # Widen both ends (floor low, ceil high) so integer rounding can only ADD comparisons, never
            # skip a real match: jaccard(a, b) <= min/max, so a survivor outside this band cannot reach it.
            low_bound = int(threshold * size)
            high_bound = int(size / threshold) + 1
            feasible_sizes = [s for s in by_size if low_bound <= s <= high_bound]
        # Candidate survivor indices in insertion order, so the first threshold match matches the baseline.
        candidate_indices = sorted(idx for s in feasible_sizes for idx in by_size[s])
        match_id: str | None = None
        for idx in candidate_indices:
            survivor_id, survivor_set = survivors[idx]
            if jaccard(fingerprint_set, survivor_set) >= threshold:
                match_id = survivor_id
                break
        if match_id is None:
            survivors.append((doc.doc_id, fingerprint_set))
            by_size.setdefault(size, []).append(len(survivors) - 1)
            kept.append(doc.doc_id)
            members[doc.doc_id] = [doc.doc_id]
        else:
            dropped.append(doc.doc_id)
            members[match_id].append(doc.doc_id)
    clusters = tuple(tuple(members[doc_id]) for doc_id in kept if len(members[doc_id]) > 1)
    return DedupResult(kept_ids=tuple(kept), dropped_ids=tuple(dropped), clusters=clusters)
