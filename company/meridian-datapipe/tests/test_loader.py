from __future__ import annotations

import pytest

from meridian_datapipe.errors import IngestError
from meridian_datapipe.loader import StreamingLoader
from meridian_datapipe.types import Document


def _corpus(n: int) -> list[Document]:
    return [Document(f"d{i}", f"text {i}") for i in range(n)]


def test_loader_preserves_order() -> None:
    corpus = _corpus(10)
    loaded = list(StreamingLoader(corpus, prefetch=3))
    assert [d.doc_id for d in loaded] == [d.doc_id for d in corpus]


def test_loader_yields_everything() -> None:
    assert len(list(StreamingLoader(_corpus(7), prefetch=2))) == 7


def test_loader_prefetch_one() -> None:
    loaded = list(StreamingLoader(_corpus(4), prefetch=1))
    assert [d.doc_id for d in loaded] == ["d0", "d1", "d2", "d3"]


def test_loader_prefetch_exceeds_source() -> None:
    loaded = list(StreamingLoader(_corpus(2), prefetch=10))
    assert len(loaded) == 2


def test_loader_rejects_bad_prefetch() -> None:
    with pytest.raises(IngestError):
        StreamingLoader(_corpus(1), prefetch=0)


def test_loader_consumes_a_generator() -> None:
    source = (Document(f"g{i}", str(i)) for i in range(5))
    loaded = list(StreamingLoader(source, prefetch=2))
    assert [d.doc_id for d in loaded] == ["g0", "g1", "g2", "g3", "g4"]


def test_loader_batches() -> None:
    batches = list(StreamingLoader(_corpus(7), prefetch=2).batches(3))
    assert [len(b) for b in batches] == [3, 3, 1]


def test_loader_batches_rejects_bad_size() -> None:
    with pytest.raises(IngestError):
        list(StreamingLoader(_corpus(3)).batches(0))


def test_loader_take() -> None:
    taken = StreamingLoader(_corpus(10), prefetch=4).take(3)
    assert [d.doc_id for d in taken] == ["d0", "d1", "d2"]


def test_loader_take_more_than_available() -> None:
    taken = StreamingLoader(_corpus(2)).take(10)
    assert len(taken) == 2


def test_loader_take_rejects_negative() -> None:
    with pytest.raises(IngestError):
        StreamingLoader(_corpus(2)).take(-1)


def test_loader_empty_source() -> None:
    assert list(StreamingLoader([], prefetch=3)) == []
