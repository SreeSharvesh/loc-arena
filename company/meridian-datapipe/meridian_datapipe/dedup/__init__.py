"""Deduplication: exact content dedup, a pairwise near-duplicate baseline, and MinHash + LSH near-dedup."""

from __future__ import annotations

from meridian_datapipe.dedup.exact import ExactDeduplicator, exact_dedup
from meridian_datapipe.dedup.minhash import LshIndex, MinHasher, lsh_dedup
from meridian_datapipe.dedup.near import dedup, jaccard, shingles
from meridian_datapipe.dedup.result import DedupResult

__all__ = [
    "DedupResult",
    "ExactDeduplicator",
    "LshIndex",
    "MinHasher",
    "dedup",
    "exact_dedup",
    "jaccard",
    "lsh_dedup",
    "shingles",
]
