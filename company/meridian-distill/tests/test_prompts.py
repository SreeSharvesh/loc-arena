from __future__ import annotations

import pytest

from meridian_datapipe.types import Document
from meridian_distill.errors import PromptError
from meridian_distill.prompts import PromptTemplate, RenderedPrompt


def _doc(doc_id: str, text: str) -> Document:
    return Document(doc_id, text)


def test_render_is_deterministic() -> None:
    tmpl = PromptTemplate(instruction="summarize")
    doc = _doc("d1", "the quick brown fox")
    a = tmpl.render(doc)
    b = tmpl.render(doc)
    assert a == b
    assert isinstance(a, RenderedPrompt)


def test_render_token_ids_within_vocab() -> None:
    tmpl = PromptTemplate(vocab_size=16)
    prompt = tmpl.render(_doc("d1", "hello world"))
    assert prompt.token_ids
    assert all(0 <= t < 16 for t in prompt.token_ids)


def test_few_shot_changes_the_prompt() -> None:
    tmpl = PromptTemplate()
    doc = _doc("d1", "target text here")
    example = _doc("e1", "an example document")
    plain = tmpl.render(doc)
    shot = tmpl.render(doc, [example])
    assert plain.token_ids != shot.token_ids
    assert plain.length < shot.length


def test_distinct_documents_render_distinctly() -> None:
    tmpl = PromptTemplate()
    a = tmpl.render(_doc("d1", "alpha content"))
    b = tmpl.render(_doc("d2", "beta content"))
    assert a.token_ids != b.token_ids


def test_empty_document_is_rejected() -> None:
    with pytest.raises(PromptError):
        PromptTemplate().render(_doc("d1", ""))


def test_tiny_vocab_is_rejected() -> None:
    with pytest.raises(PromptError):
        PromptTemplate(vocab_size=1)


def test_render_text_includes_target_and_examples() -> None:
    tmpl = PromptTemplate(instruction="do it")
    text = tmpl.render_text(_doc("d9", "body"), [_doc("e0", "example body")])
    assert "do it" in text
    assert "[target d9]" in text
    assert "[example e0]" in text
