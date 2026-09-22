"""The tokenizer vocabulary: a stable, bijective map between word tokens and integer ids.

Ids ``0..255`` are reserved for the byte fallback (any token absent from the vocabulary is emitted as its raw
UTF-8 bytes), so learned word tokens are numbered from :data:`BYTE_BASE` upward. A vocabulary built from a
corpus orders its tokens by descending frequency with the token string as a deterministic tie-break, so the
same corpus always yields the same id assignment regardless of input document order.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from meridian_datapipe.tokenize.segment import segment

BYTE_BASE: int = 256


class Vocabulary:
    """A bijective map between word tokens and ids ``>= BYTE_BASE`` (byte ids handled by the tokenizer)."""

    def __init__(self, tokens: Iterable[str] = ()) -> None:
        """Build a vocabulary assigning consecutive ids from :data:`BYTE_BASE` to the tokens in order."""
        self._id_of: dict[str, int] = {}
        self._token_of: dict[int, str] = {}
        for token in tokens:
            self.add(token)

    def add(self, token: str) -> int:
        """Add ``token`` if absent and return its id (idempotent for an already-known token)."""
        if token in self._id_of:
            return self._id_of[token]
        token_id = BYTE_BASE + len(self._id_of)
        self._id_of[token] = token_id
        self._token_of[token_id] = token
        return token_id

    def __len__(self) -> int:
        """The number of word tokens in the vocabulary."""
        return len(self._id_of)

    def __contains__(self, token: str) -> bool:
        """Whether ``token`` has an id in the vocabulary."""
        return token in self._id_of

    def id_of(self, token: str) -> int | None:
        """The id for ``token``, or ``None`` if it is not a vocabulary token."""
        return self._id_of.get(token)

    def token_of(self, token_id: int) -> str | None:
        """The token for a vocabulary id, or ``None`` if the id is not a word-token id."""
        return self._token_of.get(token_id)

    def tokens(self) -> list[str]:
        """The vocabulary tokens in id order."""
        return [self._token_of[i] for i in sorted(self._token_of)]

    @classmethod
    def from_texts(cls, texts: Iterable[str], *, max_size: int = 4096, min_count: int = 1) -> Vocabulary:
        """Build a vocabulary from a corpus, keeping the most frequent tokens up to ``max_size``.

        Tokens are ranked by descending frequency with the token string as a deterministic tie-break, and only
        tokens seen at least ``min_count`` times are eligible.
        """
        counts: Counter[str] = Counter()
        for text in texts:
            for token in segment(text):
                if not token.isspace():
                    counts[token] += 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        vocab = cls()
        for token, count in ranked:
            if count < min_count or len(vocab) >= max_size:
                continue
            vocab.add(token)
        return vocab
