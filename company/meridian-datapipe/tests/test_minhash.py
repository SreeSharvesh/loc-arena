from __future__ import annotations

import pytest

from meridian_datapipe.dedup.minhash import LshIndex, MinHasher, lsh_dedup
from meridian_datapipe.errors import DedupError
from meridian_datapipe.types import Document


def _docs(*pairs: tuple[str, str]) -> list[Document]:
    return [Document(doc_id, text) for doc_id, text in pairs]


def test_minhash_signature_is_deterministic() -> None:
    h = MinHasher(num_perm=32, seed=7)
    assert h.signature("the quick brown fox") == h.signature("the quick brown fox")


def test_minhash_signature_length() -> None:
    h = MinHasher(num_perm=48, seed=1)
    assert len(h.signature("some sample text here")) == 48


def test_minhash_identical_texts_full_similarity() -> None:
    h = MinHasher(num_perm=64, seed=3)
    sig = h.signature("alpha beta gamma delta epsilon")
    assert h.estimated_jaccard(sig, sig) == 1.0


def test_minhash_estimates_track_similarity() -> None:
    h = MinHasher(num_perm=128, seed=0)
    near = h.estimated_jaccard(
        h.signature("the quick brown fox jumps over the lazy dog"),
        h.signature("the quick brown fox jumps over the lazy cat"),
    )
    far = h.estimated_jaccard(
        h.signature("the quick brown fox jumps over the lazy dog"),
        h.signature("completely different words with nothing in common at all"),
    )
    assert near > far


def test_minhash_rejects_bad_num_perm() -> None:
    with pytest.raises(DedupError):
        MinHasher(num_perm=0)


def test_minhash_length_mismatch_raises() -> None:
    h = MinHasher(num_perm=8, seed=0)
    with pytest.raises(DedupError):
        h.estimated_jaccard((1, 2, 3), (1, 2))


def test_lsh_index_finds_candidate_pairs() -> None:
    index = LshIndex(bands=4, rows=2)
    sig = tuple(range(8))
    pairs = index.candidate_pairs({"a": sig, "b": sig, "c": tuple(range(100, 108))})
    assert ("a", "b") in pairs
    assert ("a", "c") not in pairs


def test_lsh_index_rejects_wrong_signature_length() -> None:
    index = LshIndex(bands=2, rows=2)
    with pytest.raises(DedupError):
        index.candidate_pairs({"a": (1, 2, 3)})


def test_lsh_index_rejects_bad_geometry() -> None:
    with pytest.raises(DedupError):
        LshIndex(bands=0, rows=2)


def test_lsh_dedup_folds_duplicates() -> None:
    docs = _docs(
        ("a", "the quick brown fox jumps over the lazy dog"),
        ("b", "the quick brown fox jumps over the lazy dog"),
        ("c", "totally unrelated content about maritime navigation systems"),
    )
    result = lsh_dedup(docs, threshold=0.5, bands=16, rows=4, seed=0)
    assert result.kept_ids == ("a", "c")
    assert result.dropped_ids == ("b",)


def test_lsh_dedup_keeps_distinct_docs() -> None:
    docs = _docs(
        ("a", "apples oranges bananas grapes"),
        ("b", "engines turbines pistons valves"),
    )
    result = lsh_dedup(docs, threshold=0.5, bands=16, rows=4, seed=0)
    assert result.unique_count == 2


def test_lsh_dedup_rejects_bad_threshold() -> None:
    with pytest.raises(DedupError):
        lsh_dedup(_docs(("a", "x")), threshold=-0.1)


def test_lsh_dedup_matches_exact_duplicate_cluster() -> None:
    docs = _docs(
        ("a", "shared phrase repeated across documents here"),
        ("b", "shared phrase repeated across documents here"),
        ("c", "shared phrase repeated across documents here"),
    )
    result = lsh_dedup(docs, threshold=0.6, bands=16, rows=4, seed=0)
    assert result.clusters == (("a", "b", "c"),)
