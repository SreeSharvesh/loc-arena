from __future__ import annotations

import dataclasses

import pytest

from meridian_datapipe.errors import ShardError
from meridian_datapipe.shard import ManifestRegistry, Sharder, assign_shard, shard_documents
from meridian_datapipe.types import Document


def _corpus(n: int) -> list[Document]:
    return [Document(f"d{i}", f"text {i}") for i in range(n)]


def test_assign_shard_is_deterministic() -> None:
    assert assign_shard("d5", 8) == assign_shard("d5", 8)


def test_assign_shard_in_range() -> None:
    for i in range(50):
        assert 0 <= assign_shard(f"d{i}", 4) < 4


def test_assign_shard_rejects_bad_count() -> None:
    with pytest.raises(ShardError):
        assign_shard("d1", 0)


def test_sharder_partitions_all_documents() -> None:
    manifest = shard_documents(_corpus(30), 4)
    assert manifest.num_shards == 4
    assert manifest.total_docs == 30


def test_sharding_is_stable_across_order() -> None:
    corpus = _corpus(20)
    a = shard_documents(corpus, 4)
    b = shard_documents(list(reversed(corpus)), 4)
    assert a.checksum == b.checksum


def test_sharder_assign_matches_membership() -> None:
    sharder = Sharder(4)
    manifest = sharder.shard(_corpus(20))
    for shard in manifest.shards:
        for doc_id in shard.doc_ids:
            assert sharder.assign(doc_id) == shard.shard_id


def test_sharder_rejects_bad_count() -> None:
    with pytest.raises(ShardError):
        Sharder(0)


def test_manifest_registry_register_and_get() -> None:
    manifest = shard_documents(_corpus(10), 3)
    registry = ManifestRegistry()
    registry.register("aurora", manifest)
    assert "aurora" in registry
    assert registry.get("aurora") is manifest


def test_manifest_registry_locate() -> None:
    manifest = shard_documents(_corpus(15), 3)
    registry = ManifestRegistry()
    registry.register("ds", manifest)
    expected = Sharder(3).assign("d7")
    assert registry.locate("ds", "d7") == expected


def test_manifest_registry_locate_missing_doc() -> None:
    registry = ManifestRegistry()
    registry.register("ds", shard_documents(_corpus(3), 2))
    with pytest.raises(ShardError):
        registry.locate("ds", "nope")


def test_manifest_registry_get_missing() -> None:
    with pytest.raises(ShardError):
        ManifestRegistry().get("absent")


def test_manifest_registry_rejects_tampered_manifest() -> None:
    manifest = shard_documents(_corpus(6), 2)
    bad_shard = dataclasses.replace(manifest.shards[0], checksum="tampered")
    tampered = dataclasses.replace(manifest, shards=(bad_shard,) + manifest.shards[1:])
    registry = ManifestRegistry()
    assert not registry.verify(tampered)
    with pytest.raises(ShardError):
        registry.register("bad", tampered)


def test_manifest_verify_detects_count_mismatch() -> None:
    manifest = shard_documents(_corpus(6), 2)
    broken = dataclasses.replace(manifest, num_shards=3)
    assert not ManifestRegistry().verify(broken)
