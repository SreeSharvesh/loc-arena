"""The holdout registry: the set of content hashes a corpus must not contain, held across shards.

A holdout (the eval/test documents that training data must exclude) can be large, so its content hashes are
kept as one or more shards rather than a single set. A registry built with one shard holds every hash in that
shard; a registry built with several splits the hashes across that many shards. Membership
(:meth:`__contains__`) is defined over the whole holdout, regardless of how many shards it is split into.
"""

from __future__ import annotations

from collections.abc import Iterable

from meridian_datapipe.errors import ContaminationError
from meridian_datapipe.types import Document


class HoldoutRegistry:
    """The holdout content hashes, partitioned into one or more shards."""

    def __init__(self, shards: Iterable[frozenset[str]]) -> None:
        """Hold the shard tuple (at least one shard is required)."""
        shard_tuple = tuple(shards)
        if not shard_tuple:
            raise ContaminationError("registry needs at least one shard", code="datapipe.contamination")
        self._shards = shard_tuple

    @property
    def shards(self) -> tuple[frozenset[str], ...]:
        """The holdout shards, in order."""
        return self._shards

    @property
    def num_shards(self) -> int:
        """The number of shards the holdout is split across."""
        return len(self._shards)

    def __len__(self) -> int:
        """The total number of distinct holdout hashes across all shards."""
        return len(self.all_hashes())

    def __contains__(self, content_hash: str) -> bool:
        """Whether ``content_hash`` is registered as holdout in any shard."""
        return any(content_hash in shard for shard in self._shards)

    def all_hashes(self) -> frozenset[str]:
        """The union of every shard's hashes."""
        merged: set[str] = set()
        for shard in self._shards:
            merged |= shard
        return frozenset(merged)

    def shard(self, index: int) -> frozenset[str]:
        """The shard at ``index`` (raises :class:`IndexError` if out of range)."""
        return self._shards[index]

    @classmethod
    def from_hashes(cls, hashes: Iterable[str], *, num_shards: int = 1) -> HoldoutRegistry:
        """Build a registry by splitting deduplicated, ordered hashes into ``num_shards`` contiguous parts."""
        if num_shards < 1:
            raise ContaminationError("num_shards must be >= 1", code="datapipe.contamination")
        ordered = sorted(set(hashes))
        buckets: list[list[str]] = [[] for _ in range(num_shards)]
        for i, value in enumerate(ordered):
            buckets[(i * num_shards) // max(1, len(ordered))].append(value)
        return cls(frozenset(bucket) for bucket in buckets)

    @classmethod
    def from_documents(cls, docs: Iterable[Document], *, num_shards: int = 1) -> HoldoutRegistry:
        """Build a registry from documents by hashing each document's text (see :func:`content_hash`)."""
        from meridian_datapipe.contamination.filter import content_hash

        return cls.from_hashes((content_hash(doc.text) for doc in docs), num_shards=num_shards)
