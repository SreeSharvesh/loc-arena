"""A deterministic, reversible word/byte tokenizer.

Text is segmented into word, whitespace, and punctuation tokens (see :mod:`segment`). A token present in the
vocabulary encodes to its single word-token id; any other token falls back to its UTF-8 bytes, each emitted
as an id in ``0..255``. Decoding inverts this exactly: word-token ids expand back to their strings and runs of
byte ids are decoded as UTF-8, so ``decode(encode(text)) == text`` for every input. Encoding depends only on
the text and the vocabulary, never on call order, so it is fully deterministic.
"""

from __future__ import annotations

from meridian_common import cost
from meridian_datapipe.errors import TokenizeError
from meridian_datapipe.tokenize.segment import segment
from meridian_datapipe.tokenize.vocab import BYTE_BASE, Vocabulary


class Tokenizer:
    """Encodes text to token ids and decodes token ids back to text against a fixed :class:`Vocabulary`."""

    def __init__(self, vocab: Vocabulary | None = None) -> None:
        """Hold the vocabulary (an empty one, i.e. pure byte fallback, when none is given)."""
        self._vocab = vocab if vocab is not None else Vocabulary()

    @property
    def vocab(self) -> Vocabulary:
        """The vocabulary this tokenizer encodes against."""
        return self._vocab

    def encode(self, text: str) -> list[int]:
        """Encode ``text`` to a list of token ids (word-token ids for known tokens, byte ids otherwise)."""
        ids: list[int] = []
        for token in segment(text):
            token_id = self._vocab.id_of(token)
            if token_id is not None:
                ids.append(token_id)
            else:
                ids.extend(token.encode("utf-8"))
        cost.record("datapipe.tokens", len(ids))  # per-token throughput accounting
        return ids

    def decode(self, ids: list[int]) -> str:
        """Decode a list of token ids back to text (raises :class:`TokenizeError` on an unknown id)."""
        parts: list[str] = []
        byte_buffer = bytearray()
        for token_id in ids:
            if token_id < 0:
                raise TokenizeError("token id must be non-negative", code="datapipe.tokenize", id=token_id)
            if token_id < BYTE_BASE:
                byte_buffer.append(token_id)
                continue
            if byte_buffer:
                parts.append(byte_buffer.decode("utf-8"))
                byte_buffer.clear()
            token = self._vocab.token_of(token_id)
            if token is None:
                raise TokenizeError("unknown token id", code="datapipe.tokenize", id=token_id)
            parts.append(token)
        if byte_buffer:
            parts.append(byte_buffer.decode("utf-8"))
        return "".join(parts)

    def count(self, text: str) -> int:
        """The number of token ids ``text`` encodes to."""
        return len(self.encode(text))
