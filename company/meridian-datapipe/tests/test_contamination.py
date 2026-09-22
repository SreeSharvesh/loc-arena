from __future__ import annotations

import pytest

from meridian_datapipe.contamination import ContaminationFilter, HoldoutRegistry, content_hash
from meridian_datapipe.errors import ContaminationError
from meridian_datapipe.types import Document


def _holdout(*texts: str) -> list[Document]:
    return [Document(f"h{i}", text) for i, text in enumerate(texts)]


def test_content_hash_is_stable() -> None:
    assert content_hash("some text") == content_hash("some text")
    assert content_hash("a") != content_hash("b")


def test_registry_requires_a_shard() -> None:
    with pytest.raises(ContaminationError):
        HoldoutRegistry([])


def test_registry_rejects_bad_num_shards() -> None:
    with pytest.raises(ContaminationError):
        HoldoutRegistry.from_hashes(["a", "b"], num_shards=0)


def test_registry_membership_over_union() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("alpha", "beta", "gamma"), num_shards=3)
    for text in ("alpha", "beta", "gamma"):
        assert content_hash(text) in reg
    assert content_hash("delta") not in reg


def test_registry_all_hashes_unions_shards() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("a", "b", "c", "d"), num_shards=2)
    assert len(reg.all_hashes()) == 4
    assert len(reg) == 4


def test_registry_from_hashes_dedups() -> None:
    reg = HoldoutRegistry.from_hashes(["x", "x", "y"], num_shards=1)
    assert len(reg) == 2


def test_filter_detects_single_shard_contamination() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("secret eval item"), num_shards=1)
    filt = ContaminationFilter(reg)
    assert filt.is_contaminated(Document("x", "secret eval item"))
    assert not filt.is_contaminated(Document("y", "innocuous training text"))


def test_filter_drops_contaminated_documents() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("held out one", "held out two"), num_shards=1)
    filt = ContaminationFilter(reg)
    corpus = [
        Document("a", "held out one"),
        Document("b", "clean sample"),
        Document("c", "held out two"),
    ]
    clean = filt.filter(corpus)
    assert [d.doc_id for d in clean] == ["b"]


def test_filter_partition_splits_corpus() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("contam"), num_shards=1)
    filt = ContaminationFilter(reg)
    corpus = [Document("a", "contam"), Document("b", "fresh")]
    clean, contaminated = filt.partition(corpus)
    assert [d.doc_id for d in clean] == ["b"]
    assert [d.doc_id for d in contaminated] == ["a"]


def test_contamination_rate() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("dirty"), num_shards=1)
    filt = ContaminationFilter(reg)
    corpus = [Document("a", "dirty"), Document("b", "ok"), Document("c", "dirty"), Document("d", "ok")]
    assert filt.contamination_rate(corpus) == pytest.approx(0.5)


def test_contamination_rate_empty_is_zero() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("x"), num_shards=1)
    filt = ContaminationFilter(reg)
    assert filt.contamination_rate([]) == 0.0


def test_registry_exposes_shard_geometry() -> None:
    reg = HoldoutRegistry.from_documents(_holdout("a", "b", "c", "d"), num_shards=2)
    assert reg.num_shards == 2
    assert sum(len(shard) for shard in reg.shards) == 4
