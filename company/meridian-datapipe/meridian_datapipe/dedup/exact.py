"""Exact deduplication: fold documents with identical content onto their first occurrence.

Each document is reduced to a content signature; documents sharing a signature count as the same content,
and only the first occurrence in input order survives while the rest are dropped and recorded in that
signature's cluster. Two documents with genuinely different content must land on different signatures, so
distinct texts are never merged.
"""

from __future__ import annotations

from meridian_common.serde.canonical import fingerprint
from meridian_datapipe.dedup.result import DedupResult
from meridian_datapipe.types import Document

_HEAD_CHARS = 64


def _content_signature(text: str) -> str:
    """The content signature used to group exact duplicates: the text length and a hash of its head."""
    head = text[:_HEAD_CHARS]
    return f"{len(text)}:{fingerprint(head)}"


class ExactDeduplicator:
    """Groups documents by content signature and keeps the first occurrence of each."""

    def dedup(self, docs: list[Document]) -> DedupResult:
        """Drop later documents whose content signature matches an earlier one; keep first occurrences."""
        first_of: dict[str, str] = {}
        members: dict[str, list[str]] = {}
        kept: list[str] = []
        dropped: list[str] = []
        for doc in docs:
            sig = _content_signature(doc.text)
            if sig in first_of:
                dropped.append(doc.doc_id)
                members[first_of[sig]].append(doc.doc_id)
            else:
                first_of[sig] = doc.doc_id
                members[doc.doc_id] = [doc.doc_id]
                kept.append(doc.doc_id)
        clusters = tuple(tuple(members[doc_id]) for doc_id in kept if len(members[doc_id]) > 1)
        return DedupResult(kept_ids=tuple(kept), dropped_ids=tuple(dropped), clusters=clusters)


def exact_dedup(docs: list[Document]) -> DedupResult:
    """Convenience: exact-dedup a document list with a one-shot :class:`ExactDeduplicator`."""
    return ExactDeduplicator().dedup(docs)
