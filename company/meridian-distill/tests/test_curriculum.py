from __future__ import annotations

import pytest

from meridian_datapipe.shard import shard_documents
from meridian_datapipe.types import Document
from meridian_distill.curriculum import CurriculumBuilder, assemble_curriculum
from meridian_distill.errors import CurriculumError


def _docs() -> list[Document]:
    return [
        Document("d1", "x" * 30),
        Document("d2", "x" * 10),
        Document("d3", "x" * 20),
    ]


def test_orders_by_ascending_length() -> None:
    curriculum = assemble_curriculum(_docs())
    assert [item.doc_id for item in curriculum] == ["d2", "d3", "d1"]


def test_ordering_is_stable_across_input_order() -> None:
    docs = _docs()
    a = assemble_curriculum(docs)
    b = assemble_curriculum(list(reversed(docs)))
    assert [i.doc_id for i in a] == [i.doc_id for i in b]


def test_documents_property_matches_order() -> None:
    curriculum = assemble_curriculum(_docs())
    assert [d.doc_id for d in curriculum.documents] == ["d2", "d3", "d1"]


def test_by_meta_orders_by_numeric_field() -> None:
    docs = [
        Document("a", "body", meta={"score": 3.0}),
        Document("b", "body", meta={"score": 1.0}),
        Document("c", "body", meta={"score": 2.0}),
    ]
    curriculum = CurriculumBuilder.by_meta("score").build(docs)
    assert [i.doc_id for i in curriculum] == ["b", "c", "a"]


def test_by_meta_rejects_non_numeric() -> None:
    docs = [Document("a", "body", meta={"score": "high"})]
    with pytest.raises(CurriculumError):
        CurriculumBuilder.by_meta("score").build(docs)


def test_empty_corpus_is_rejected() -> None:
    with pytest.raises(CurriculumError):
        assemble_curriculum([])


def test_from_shard_uses_shard_membership() -> None:
    docs = _docs()
    manifest = shard_documents(docs, 1)
    lookup = {d.doc_id: d for d in docs}
    curriculum = CurriculumBuilder().from_shard(manifest.shard(0), lookup)
    assert len(curriculum) == 3
    assert {i.doc_id for i in curriculum} == {"d1", "d2", "d3"}


def test_from_shard_rejects_unknown_member() -> None:
    docs = _docs()
    manifest = shard_documents(docs, 1)
    with pytest.raises(CurriculumError):
        CurriculumBuilder().from_shard(manifest.shard(0), {})
