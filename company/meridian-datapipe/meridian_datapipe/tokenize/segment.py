"""Text segmentation: split text into a lossless sequence of word, whitespace, and punctuation tokens.

The segmentation is exact: concatenating the returned tokens reproduces the input string byte-for-byte, which
is what lets the tokenizer round-trip. Runs of alphanumeric characters form word tokens, runs of whitespace
form whitespace tokens, and every other character is its own single-character token.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+|\s+|[^0-9A-Za-z_\s]", re.UNICODE)


def segment(text: str) -> list[str]:
    """Split ``text`` into word / whitespace / punctuation tokens whose concatenation equals ``text``."""
    return _TOKEN_RE.findall(text)
