"""MinHash signatures with LSH banding for scalable near-duplicate detection.

A :class:`MinHasher` maps a document's shingle set to a fixed-length integer signature such that the expected
fraction of equal signature positions between two documents approximates their Jaccard similarity. An
:class:`LshIndex` bands those signatures so that documents sharing any band bucket become candidate pairs,
which are then confirmed by their signature-estimated similarity. Both are deterministic given the seed and
band geometry.
"""

from __future__ import annotations

import hashlib

from meridian_datapipe.dedup.near import shingles
from meridian_datapipe.dedup.result import DedupResult
from meridian_datapipe.errors import DedupError
from meridian_datapipe.types import Document

_MERSENNE_PRIME = (1 << 61) - 1
_MAX_HASH = (1 << 32) - 1


def _base_hash(shingle: str) -> int:
    """A stable 32-bit base hash of a shingle string."""
    digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & _MAX_HASH


class MinHasher:
    """Produces fixed-length MinHash signatures over shingle sets, deterministically from a seed."""

    def __init__(self, num_perm: int = 64, *, seed: int = 0, k: int = 3) -> None:
        """Build ``num_perm`` seeded linear hash permutations and hold the shingle size."""
        if num_perm < 1:
            raise DedupError("num_perm must be >= 1", code="datapipe.dedup", num_perm=num_perm)
        self._num_perm = num_perm
        self._k = k
        rng = _LinearRng(seed)
        self._a = [rng.next_nonzero() for _ in range(num_perm)]
        self._b = [rng.next() for _ in range(num_perm)]

    @property
    def num_perm(self) -> int:
        """The signature length (number of hash permutations)."""
        return self._num_perm

    def signature(self, text: str) -> tuple[int, ...]:
        """The MinHash signature of ``text`` (all-max for an empty shingle set)."""
        shingle_set = shingles(text, self._k)
        if not shingle_set:
            return tuple(_MERSENNE_PRIME for _ in range(self._num_perm))
        base = [_base_hash(s) for s in shingle_set]
        sig: list[int] = []
        for a, b in zip(self._a, self._b, strict=True):
            sig.append(min((a * h + b) % _MERSENNE_PRIME for h in base))
        return tuple(sig)

    def estimated_jaccard(self, sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
        """Estimate the Jaccard similarity of two documents from equal positions in their signatures."""
        if len(sig_a) != len(sig_b):
            raise DedupError("signatures differ in length", code="datapipe.dedup")
        if not sig_a:
            return 1.0
        equal = sum(1 for x, y in zip(sig_a, sig_b, strict=True) if x == y)
        return equal / len(sig_a)


class _LinearRng:
    """A tiny deterministic LCG used only to derive stable hash coefficients."""

    def __init__(self, seed: int) -> None:
        """Seed the generator."""
        self._state = (seed % _MERSENNE_PRIME) or 1

    def next(self) -> int:
        """The next pseudo-random value in ``[0, prime)``."""
        self._state = (6364136223846793005 * self._state + 1442695040888963407) % _MERSENNE_PRIME
        return self._state

    def next_nonzero(self) -> int:
        """The next pseudo-random value in ``[1, prime)``."""
        value = self.next()
        return value if value != 0 else 1


class LshIndex:
    """Bands MinHash signatures into buckets so co-bucketed documents become near-duplicate candidates."""

    def __init__(self, bands: int, rows: int) -> None:
        """Hold the band count and rows-per-band (the signature length is ``bands * rows``)."""
        if bands < 1 or rows < 1:
            raise DedupError("bands and rows must be >= 1", code="datapipe.dedup", bands=bands, rows=rows)
        self._bands = bands
        self._rows = rows

    @property
    def num_perm(self) -> int:
        """The signature length this banding expects."""
        return self._bands * self._rows

    def candidate_pairs(self, signatures: dict[str, tuple[int, ...]]) -> set[tuple[str, str]]:
        """The set of unordered doc-id pairs that share at least one band bucket."""
        buckets: dict[tuple[int, int, tuple[int, ...]], list[str]] = {}
        for doc_id, sig in signatures.items():
            if len(sig) != self.num_perm:
                raise DedupError("signature length does not match banding", code="datapipe.dedup")
            for band in range(self._bands):
                chunk = sig[band * self._rows : (band + 1) * self._rows]
                buckets.setdefault((band, self._rows, chunk), []).append(doc_id)
        pairs: set[tuple[str, str]] = set()
        for members in buckets.values():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    pairs.add(tuple(sorted((members[i], members[j]))))  # type: ignore[arg-type]
        return pairs


def lsh_dedup(
    docs: list[Document],
    threshold: float = 0.8,
    *,
    bands: int = 16,
    rows: int = 4,
    seed: int = 0,
    k: int = 3,
) -> DedupResult:
    """Near-dedup a corpus with MinHash + LSH, keeping the first occurrence of each near-duplicate group.

    Signatures are banded into candidate pairs; a candidate is confirmed when its signature-estimated Jaccard
    similarity meets ``threshold``. Confirmed near-duplicates are folded onto the earliest surviving document
    in input order.
    """
    if not 0.0 <= threshold <= 1.0:
        raise DedupError("threshold must be in [0, 1]", code="datapipe.dedup", threshold=threshold)
    hasher = MinHasher(bands * rows, seed=seed, k=k)
    index = LshIndex(bands, rows)
    signatures = {doc.doc_id: hasher.signature(doc.text) for doc in docs}
    order = {doc.doc_id: i for i, doc in enumerate(docs)}

    neighbors: dict[str, set[str]] = {doc.doc_id: set() for doc in docs}
    for left, right in index.candidate_pairs(signatures):
        if hasher.estimated_jaccard(signatures[left], signatures[right]) >= threshold:
            neighbors[left].add(right)
            neighbors[right].add(left)

    kept: list[str] = []
    dropped: list[str] = []
    folded_into: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for doc in docs:
        earlier = [folded_into.get(n, n) for n in neighbors[doc.doc_id] if order[n] < order[doc.doc_id]]
        if earlier:
            target = min(earlier, key=lambda doc_id: order[doc_id])
            dropped.append(doc.doc_id)
            folded_into[doc.doc_id] = target
            members[target].append(doc.doc_id)
        else:
            kept.append(doc.doc_id)
            members[doc.doc_id] = [doc.doc_id]
    clusters = tuple(tuple(members[doc_id]) for doc_id in kept if len(members[doc_id]) > 1)
    return DedupResult(kept_ids=tuple(kept), dropped_ids=tuple(dropped), clusters=clusters)
