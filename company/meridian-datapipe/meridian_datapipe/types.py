"""Core value types shared across the data platform.

A :class:`Document` is one corpus text unit (a stable id, its text, a source label, and free-form metadata).
A :class:`Shard` names a deterministic partition of documents plus a checksum over its membership. A
:class:`Manifest` is the full record of a sharding: every shard, the shard count, and a top-level checksum.
All three are immutable and dependency-free so every datapipe module agrees on the shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class Document:
    """One corpus document: a stable id, its text, a source label, and free-form metadata.

    ``meta`` is stored as a read-only mapping so a frozen document cannot be mutated through it. Use
    :meth:`with_text` or :meth:`with_meta` to derive a changed copy rather than mutating in place.
    """

    doc_id: str
    text: str
    source: str = "unknown"
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze ``meta`` behind a read-only view so the frozen document is truly immutable."""
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))

    @property
    def length(self) -> int:
        """The document text length in characters."""
        return len(self.text)

    def with_text(self, text: str) -> Document:
        """A copy of this document with replaced text (same id, source, and metadata)."""
        return Document(doc_id=self.doc_id, text=text, source=self.source, meta=dict(self.meta))

    def with_meta(self, **updates: Any) -> Document:
        """A copy of this document with ``updates`` merged into its metadata."""
        merged = dict(self.meta)
        merged.update(updates)
        return Document(doc_id=self.doc_id, text=self.text, source=self.source, meta=merged)


@dataclass(frozen=True)
class Shard:
    """A deterministic partition of documents: its index, its member doc ids, and a membership checksum."""

    shard_id: int
    doc_ids: tuple[str, ...]
    checksum: str

    @property
    def size(self) -> int:
        """The number of documents assigned to this shard."""
        return len(self.doc_ids)

    def __contains__(self, doc_id: str) -> bool:
        """Whether ``doc_id`` is a member of this shard."""
        return doc_id in self.doc_ids


@dataclass(frozen=True)
class Manifest:
    """The full record of a sharding: every shard, the shard count, and a top-level checksum."""

    shards: tuple[Shard, ...]
    num_shards: int
    checksum: str

    @property
    def total_docs(self) -> int:
        """The total number of documents recorded across all shards."""
        return sum(shard.size for shard in self.shards)

    def shard(self, shard_id: int) -> Shard:
        """The shard with index ``shard_id`` (raises :class:`IndexError` if out of range)."""
        return self.shards[shard_id]
