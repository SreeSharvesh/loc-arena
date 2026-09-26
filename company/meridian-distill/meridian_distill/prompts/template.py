"""Prompt construction and templating.

A :class:`PromptTemplate` renders a document into a prompt for the teacher: an optional instruction, a
sequence of few-shot example documents, and the target document, tokenized into a deterministic token-id
sequence over a fixed vocabulary. Rendering depends only on the inputs and the template configuration, so the
same document and few-shot context always render to the same prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from meridian_datapipe.types import Document
from meridian_distill.errors import PromptError

_DEFAULT_VOCAB = 32


def _tokenize(text: str, vocab_size: int) -> tuple[int, ...]:
    """Map ``text`` to a deterministic, order-sensitive token-id sequence over ``vocab_size`` ids."""
    tokens: list[int] = []
    rolling = 0
    for ch in text:
        rolling = (rolling * 131 + ord(ch) + 1) % 1_000_003
        tokens.append(rolling % vocab_size)
    return tuple(tokens)


@dataclass(frozen=True)
class RenderedPrompt:
    """A rendered prompt: the target document id, its human-readable text, and the teacher token ids."""

    doc_id: str
    text: str
    token_ids: tuple[int, ...]

    @property
    def length(self) -> int:
        """The number of prompt tokens."""
        return len(self.token_ids)


class PromptTemplate:
    """Renders a :class:`Document` plus few-shot context into a deterministic teacher prompt."""

    def __init__(
        self,
        *,
        instruction: str = "",
        separator: str = "\n\n",
        vocab_size: int = _DEFAULT_VOCAB,
    ) -> None:
        """Configure the standing instruction, the example separator, and the token vocabulary size."""
        if vocab_size < 2:
            raise PromptError("vocab_size must be at least 2", code="distill.prompt", vocab_size=vocab_size)
        self._instruction = instruction
        self._separator = separator
        self._vocab_size = vocab_size

    @property
    def vocab_size(self) -> int:
        """The token vocabulary size the template renders into."""
        return self._vocab_size

    def render_text(self, document: Document, few_shot: Sequence[Document] = ()) -> str:
        """Render the prompt text: the instruction, each few-shot example, and the target document."""
        parts: list[str] = []
        if self._instruction:
            parts.append(self._instruction)
        for example in few_shot:
            parts.append(f"[example {example.doc_id}] {example.text}")
        parts.append(f"[target {document.doc_id}] {document.text}")
        return self._separator.join(parts)

    def render(self, document: Document, few_shot: Sequence[Document] = ()) -> RenderedPrompt:
        """Render ``document`` (with optional ``few_shot`` examples) into a :class:`RenderedPrompt`."""
        if not document.text:
            raise PromptError(
                "cannot render an empty document",
                code="distill.prompt",
                doc_id=document.doc_id,
            )
        text = self.render_text(document, few_shot)
        return RenderedPrompt(
            doc_id=document.doc_id,
            text=text,
            token_ids=_tokenize(text, self._vocab_size),
        )
