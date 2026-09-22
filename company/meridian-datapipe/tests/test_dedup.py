from __future__ import annotations

import pytest

from meridian_datapipe.dedup import DedupResult, exact_dedup
from meridian_datapipe.dedup import dedup as near_dedup
from meridian_datapipe.dedup.near import jaccard, shingles
from meridian_datapipe.errors import DedupError
from meridian_datapipe.types import Document


def _docs(*pairs: tuple[str, str]) -> list[Document]:
    return [Document(doc_id, text) for doc_id, text in pairs]


def test_exact_dedup_drops_identical_text() -> None:
    result = exact_dedup(_docs(("a", "hello"), ("b", "hello"), ("c", "world")))
    assert result.unique_count == 2
    assert result.dropped_ids == ("b",)
    assert result.kept_ids == ("a", "c")


def test_exact_dedup_keeps_distinct_short_texts() -> None:
    result = exact_dedup(_docs(("a", "one"), ("b", "two"), ("c", "three")))
    assert result.unique_count == 3
    assert result.dropped_ids == ()


def test_exact_dedup_keeps_first_occurrence() -> None:
    result = exact_dedup(_docs(("first", "dup"), ("second", "dup")))
    assert result.kept_ids == ("first",)
    assert result.dropped_ids == ("second",)


def test_exact_dedup_records_clusters() -> None:
    result = exact_dedup(_docs(("a", "x"), ("b", "x"), ("c", "x")))
    assert result.clusters == (("a", "b", "c"),)


def test_exact_dedup_empty() -> None:
    result = exact_dedup([])
    assert result.unique_count == 0 and result.duplicate_rate() == 0.0


def test_dedup_result_rates() -> None:
    result = DedupResult(kept_ids=("a",), dropped_ids=("b", "c"))
    assert result.total_count == 3
    assert result.dropped_count == 2
    assert result.duplicate_rate() == pytest.approx(2 / 3)


def test_shingles_basic() -> None:
    assert shingles("the cat sat", k=2) == frozenset({"the cat", "cat sat"})


def test_shingles_shorter_than_k() -> None:
    assert shingles("word", k=3) == frozenset({"word"})


def test_shingles_rejects_bad_k() -> None:
    with pytest.raises(DedupError):
        shingles("text", k=0)


def test_jaccard_bounds() -> None:
    assert jaccard(frozenset(), frozenset()) == 1.0
    assert jaccard(frozenset({"a"}), frozenset()) == 0.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"a"})) == pytest.approx(0.5)


def test_near_dedup_folds_near_duplicates() -> None:
    docs = _docs(
        ("a", "the quick brown fox jumps"),
        ("b", "the quick brown fox jumps over"),
        ("c", "an entirely unrelated sentence about ships"),
    )
    result = near_dedup(docs, threshold=0.5)
    assert result.kept_ids == ("a", "c")
    assert result.dropped_ids == ("b",)


def test_near_dedup_high_threshold_keeps_all() -> None:
    docs = _docs(
        ("a", "the quick brown fox jumps"),
        ("b", "the quick brown fox leaps"),
    )
    result = near_dedup(docs, threshold=0.99)
    assert result.unique_count == 2


def test_near_dedup_rejects_bad_threshold() -> None:
    with pytest.raises(DedupError):
        near_dedup(_docs(("a", "x")), threshold=1.5)


def test_near_dedup_transitive_cluster() -> None:
    docs = _docs(
        ("a", "alpha beta gamma delta"),
        ("b", "alpha beta gamma delta"),
        ("c", "alpha beta gamma delta"),
    )
    result = near_dedup(docs, threshold=0.9)
    assert result.dropped_ids == ("b", "c")
    assert result.clusters == (("a", "b", "c"),)
