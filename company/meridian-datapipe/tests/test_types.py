from __future__ import annotations

import pytest

from meridian_datapipe.types import Document, Manifest, Shard


def test_document_meta_is_read_only() -> None:
    doc = Document("a", "text", "web", {"lang": "en"})
    assert doc.meta["lang"] == "en"
    with pytest.raises(TypeError):
        doc.meta["lang"] = "fr"  # type: ignore[index]


def test_document_is_frozen() -> None:
    doc = Document("a", "text")
    with pytest.raises(AttributeError):
        doc.text = "changed"  # type: ignore[misc]


def test_document_defaults() -> None:
    doc = Document("a", "hello")
    assert doc.source == "unknown"
    assert dict(doc.meta) == {}
    assert doc.length == 5


def test_document_with_text_preserves_identity() -> None:
    doc = Document("a", "old", "web", {"k": 1})
    new = doc.with_text("new")
    assert new.doc_id == "a" and new.text == "new" and new.source == "web"
    assert dict(new.meta) == {"k": 1}
    assert doc.text == "old"


def test_document_with_meta_merges() -> None:
    doc = Document("a", "t", meta={"k": 1})
    new = doc.with_meta(k=2, extra="x")
    assert dict(new.meta) == {"k": 2, "extra": "x"}
    assert dict(doc.meta) == {"k": 1}


def test_shard_membership_and_size() -> None:
    shard = Shard(0, ("d1", "d2"), "chk")
    assert shard.size == 2
    assert "d1" in shard and "d3" not in shard


def test_manifest_totals_and_lookup() -> None:
    shards = (Shard(0, ("a",), "c0"), Shard(1, ("b", "c"), "c1"))
    manifest = Manifest(shards=shards, num_shards=2, checksum="top")
    assert manifest.total_docs == 3
    assert manifest.shard(1).doc_ids == ("b", "c")
