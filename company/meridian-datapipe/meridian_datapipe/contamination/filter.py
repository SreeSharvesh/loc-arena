"""The contamination filter: drop corpus documents that appear in the holdout registry.

A document is contaminated when its content hash is registered in the holdout. The filter resolves a
document's hash against the registry's shards and reports whether it is present, drops contaminated documents
from a corpus, and reports the contamination rate of a corpus. :func:`content_hash` defines the single, stable
content-hash function that both the registry and the filter agree on.
"""

from __future__ import annotations

from meridian_common.serde.canonical import fingerprint
from meridian_datapipe.contamination.registry import HoldoutRegistry
from meridian_datapipe.types import Document


def content_hash(text: str) -> str:
    """The stable content hash of a document's text (used by both the registry and the filter)."""
    return fingerprint(text)


class ContaminationFilter:
    """Screens documents against a :class:`HoldoutRegistry`."""

    def __init__(self, registry: HoldoutRegistry) -> None:
        """Hold the holdout registry to screen against."""
        self._registry = registry

    @property
    def registry(self) -> HoldoutRegistry:
        """The holdout registry backing this filter."""
        return self._registry

    def is_contaminated(self, doc: Document) -> bool:
        """Whether ``doc`` appears in the holdout registry."""
        digest = content_hash(doc.text)
        shard_index = int(digest, 16) % self._registry.num_shards
        return digest in self._registry.shard(shard_index)

    def filter(self, docs: list[Document]) -> list[Document]:
        """Return the documents that are not contaminated, preserving order."""
        return [doc for doc in docs if not self.is_contaminated(doc)]

    def partition(self, docs: list[Document]) -> tuple[list[Document], list[Document]]:
        """Split ``docs`` into (clean, contaminated), preserving order within each side."""
        clean: list[Document] = []
        contaminated: list[Document] = []
        for doc in docs:
            (contaminated if self.is_contaminated(doc) else clean).append(doc)
        return clean, contaminated

    def contamination_rate(self, docs: list[Document]) -> float:
        """The fraction of ``docs`` that appear in the holdout (0.0 for an empty corpus)."""
        if not docs:
            return 0.0
        hits = sum(1 for doc in docs if self.is_contaminated(doc))
        return hits / len(docs)
