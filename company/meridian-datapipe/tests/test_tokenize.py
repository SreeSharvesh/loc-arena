from __future__ import annotations

import pytest

from meridian_datapipe.errors import TokenizeError
from meridian_datapipe.tokenize import TokenizePipeline, Tokenizer, Vocabulary, token_counts
from meridian_datapipe.tokenize.segment import segment
from meridian_datapipe.tokenize.vocab import BYTE_BASE
from meridian_datapipe.types import Document


def test_segment_is_lossless() -> None:
    for text in ["hello, world!", "  spaced\tout ", "üb3r-tokens", ""]:
        assert "".join(segment(text)) == text


def test_vocab_ids_start_at_byte_base() -> None:
    v = Vocabulary(["alpha", "beta"])
    assert v.id_of("alpha") == BYTE_BASE
    assert v.id_of("beta") == BYTE_BASE + 1
    assert v.id_of("missing") is None


def test_vocab_add_is_idempotent() -> None:
    v = Vocabulary()
    first = v.add("word")
    assert v.add("word") == first
    assert len(v) == 1


def test_vocab_from_texts_is_frequency_ordered() -> None:
    v = Vocabulary.from_texts(["a a a b b c", "a b"])
    assert v.tokens()[0] == "a"  # most frequent gets the first id
    assert v.id_of("a") == BYTE_BASE


def test_vocab_from_texts_deterministic_tie_break() -> None:
    v1 = Vocabulary.from_texts(["x y", "y x"])
    v2 = Vocabulary.from_texts(["y x", "x y"])
    assert v1.tokens() == v2.tokens()


def test_vocab_from_texts_respects_max_size_and_min_count() -> None:
    v = Vocabulary.from_texts(["a a b b c"], max_size=1)
    assert len(v) == 1
    v2 = Vocabulary.from_texts(["a a b"], min_count=2)
    assert "a" in v2 and "b" not in v2


def test_tokenizer_round_trips_known_and_unknown() -> None:
    v = Vocabulary.from_texts(["the quick brown fox"])
    tk = Tokenizer(v)
    for text in ["the quick brown fox!", "unseen wörds 123", "the the the", ""]:
        assert tk.decode(tk.encode(text)) == text


def test_tokenizer_uses_word_ids_for_known_tokens() -> None:
    v = Vocabulary(["hello"])
    tk = Tokenizer(v)
    ids = tk.encode("hello")
    assert ids == [BYTE_BASE]


def test_tokenizer_byte_fallback_for_unknown() -> None:
    tk = Tokenizer(Vocabulary())
    ids = tk.encode("hi")
    assert ids == list(b"hi")


def test_tokenizer_pure_byte_round_trip() -> None:
    tk = Tokenizer()
    assert tk.decode(tk.encode("äöü!")) == "äöü!"


def test_tokenizer_count_matches_encode() -> None:
    v = Vocabulary.from_texts(["one two"])
    tk = Tokenizer(v)
    assert tk.count("one two three") == len(tk.encode("one two three"))


def test_tokenizer_rejects_negative_id() -> None:
    tk = Tokenizer()
    with pytest.raises(TokenizeError):
        tk.decode([-1])


def test_tokenizer_rejects_unknown_word_id() -> None:
    tk = Tokenizer(Vocabulary(["known"]))
    with pytest.raises(TokenizeError):
        tk.decode([BYTE_BASE + 99])


def test_pipeline_run_preserves_order_and_counts() -> None:
    v = Vocabulary.from_texts(["alpha beta"])
    pipe = TokenizePipeline(Tokenizer(v))
    docs = [Document("a", "alpha beta"), Document("b", "gamma")]
    out = pipe.run(docs)
    assert [t.doc_id for t in out] == ["a", "b"]
    assert out[0].token_count == 3  # "alpha", " ", "beta"


def test_pipeline_total_tokens() -> None:
    v = Vocabulary.from_texts(["a b"])
    pipe = TokenizePipeline(Tokenizer(v))
    docs = [Document("a", "a b"), Document("b", "a b")]
    assert pipe.total_tokens(docs) == 2 * len(Tokenizer(v).encode("a b"))


def test_pipeline_vocab_coverage() -> None:
    v = Vocabulary(["known"])
    pipe = TokenizePipeline(Tokenizer(v))
    docs = [Document("a", "known"), Document("b", "xy")]
    coverage = pipe.vocab_coverage(docs)
    assert 0.0 < coverage < 1.0


def test_token_counts_free_function() -> None:
    v = Vocabulary.from_texts(["a b"])
    tk = Tokenizer(v)
    counts = token_counts([Document("a", "a b"), Document("b", "a")], tk)
    assert set(counts) == {"a", "b"}
