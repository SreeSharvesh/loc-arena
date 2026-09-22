"""The tokenization pipeline step over a corpus of documents.

Given a tokenizer, the pipeline encodes each document's text into token ids, producing a
:class:`TokenizedDocument` per input and exposing corpus-level token statistics. Each stage encodes from the
document text, so the pipeline reflects the document corpus exactly as it stands when the stage runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_datapipe.tokenize.tokenizer import Tokenizer
from meridian_datapipe.types import Document


@dataclass(frozen=True)
class TokenizedDocument:
    """One document's tokenization: its id, its token ids, and the source text length in characters."""

    doc_id: str
    token_ids: tuple[int, ...]
    char_len: int

    @property
    def token_count(self) -> int:
        """The number of token ids this document produced."""
        return len(self.token_ids)


class TokenizePipeline:
    """Tokenizes a corpus of documents with a fixed tokenizer."""

    def __init__(self, tokenizer: Tokenizer) -> None:
        """Hold the tokenizer used for every document."""
        self._tokenizer = tokenizer

    def encode_document(self, doc: Document) -> TokenizedDocument:
        """Encode one document's text into a :class:`TokenizedDocument`."""
        ids = self._tokenizer.encode(doc.text)
        return TokenizedDocument(doc_id=doc.doc_id, token_ids=tuple(ids), char_len=doc.length)

    def run(self, docs: list[Document]) -> list[TokenizedDocument]:
        """Tokenize every document in ``docs``, preserving order."""
        return [self.encode_document(doc) for doc in docs]

    def total_tokens(self, docs: list[Document]) -> int:
        """The total number of tokens the corpus produces."""
        return sum(self._tokenizer.count(doc.text) for doc in docs)

    def vocab_coverage(self, docs: list[Document]) -> float:
        """The fraction of emitted token ids that are word-token ids rather than byte fallbacks."""
        word_ids = 0
        total = 0
        for doc in docs:
            ids = self._tokenizer.encode(doc.text)
            total += len(ids)
            word_ids += sum(1 for token_id in ids if token_id >= 256)
        return word_ids / total if total else 0.0


def token_counts(docs: list[Document], tokenizer: Tokenizer) -> dict[str, int]:
    """The per-document token count for a corpus, keyed by doc id."""
    return {doc.doc_id: tokenizer.count(doc.text) for doc in docs}
