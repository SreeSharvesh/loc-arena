"""Deterministic document sharding into a fixed number of shards.

A document is assigned to a shard by hashing its id, so the assignment depends only on the id and the shard
count, never on input order or wall-clock time: the same corpus always produces the same shards. Each shard
carries a checksum over its (sorted) membership, and the manifest carries a checksum over the shard checksums,
so a downstream consumer can verify it received exactly the shard it expects.
"""

from __future__ import annotations

from meridian_common.serde.canonical import fingerprint
from meridian_datapipe.errors import ShardError
from meridian_datapipe.types import Document, Manifest, Shard


def assign_shard(doc_id: str, num_shards: int) -> int:
    """The shard index for ``doc_id`` under ``num_shards`` (a stable hash of the id, modulo the count)."""
    if num_shards < 1:
        raise ShardError("num_shards must be >= 1", code="datapipe.shard", num_shards=num_shards)
    return int(fingerprint(doc_id), 16) % num_shards


def _shard_checksum(doc_ids: list[str]) -> str:
    """A membership checksum for a shard, stable under document order."""
    return fingerprint(sorted(doc_ids))


class Sharder:
    """Assigns documents to a fixed number of shards and builds the sharding manifest."""

    def __init__(self, num_shards: int) -> None:
        """Hold the shard count (must be at least one)."""
        if num_shards < 1:
            raise ShardError("num_shards must be >= 1", code="datapipe.shard", num_shards=num_shards)
        self._num_shards = num_shards

    @property
    def num_shards(self) -> int:
        """The number of shards documents are distributed across."""
        return self._num_shards

    def assign(self, doc_id: str) -> int:
        """The shard index for ``doc_id``."""
        return assign_shard(doc_id, self._num_shards)

    def shard(self, docs: list[Document]) -> Manifest:
        """Partition ``docs`` into shards and return the :class:`Manifest` describing the result."""
        buckets: list[list[str]] = [[] for _ in range(self._num_shards)]
        for doc in docs:
            buckets[self.assign(doc.doc_id)].append(doc.doc_id)
        shards = tuple(
            Shard(shard_id=i, doc_ids=tuple(member_ids), checksum=_shard_checksum(member_ids))
            for i, member_ids in enumerate(buckets)
        )
        checksum = fingerprint([shard.checksum for shard in shards])
        return Manifest(shards=shards, num_shards=self._num_shards, checksum=checksum)


def shard_documents(docs: list[Document], num_shards: int) -> Manifest:
    """Convenience: shard a document list with a one-shot :class:`Sharder`."""
    return Sharder(num_shards).shard(docs)
