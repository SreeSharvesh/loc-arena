from __future__ import annotations

import pytest

from meridian_datapipe.errors import IngestError
from meridian_datapipe.ingest import SchemaNormalizer, normalize_record, read_jsonl, read_records


def test_normalize_maps_aliases() -> None:
    doc = normalize_record({"id": "x1", "content": "hi", "origin": "web"})
    assert doc.doc_id == "x1" and doc.text == "hi" and doc.source == "web"


def test_normalize_strips_and_defaults_source() -> None:
    doc = normalize_record({"text": "  padded  "})
    assert doc.text == "padded"
    assert doc.source == "unknown"


def test_normalize_no_strip() -> None:
    doc = normalize_record({"text": "  keep  "}, strip=False)
    assert doc.text == "  keep  "


def test_normalize_trims_to_max_chars() -> None:
    doc = normalize_record({"text": "abcdef"}, max_chars=3)
    assert doc.text == "abc"


def test_normalize_derives_stable_id() -> None:
    a = normalize_record({"text": "same", "source": "s"})
    b = normalize_record({"text": "same", "source": "s"})
    assert a.doc_id == b.doc_id and a.doc_id.startswith("doc-")


def test_normalize_derived_id_depends_on_source() -> None:
    a = normalize_record({"text": "same", "source": "s1"})
    b = normalize_record({"text": "same", "source": "s2"})
    assert a.doc_id != b.doc_id


def test_normalize_carries_unreserved_keys_as_meta() -> None:
    doc = normalize_record({"text": "t", "lang": "en", "score": 3})
    assert dict(doc.meta) == {"lang": "en", "score": 3}


def test_normalize_missing_text_raises() -> None:
    with pytest.raises(IngestError):
        normalize_record({"source": "web"})


def test_normalize_non_string_text_raises() -> None:
    with pytest.raises(IngestError):
        normalize_record({"text": 123})


def test_normalizer_rejects_negative_max_chars() -> None:
    with pytest.raises(IngestError):
        SchemaNormalizer(max_chars=-1)


def test_read_records_preserves_order() -> None:
    docs = read_records([{"id": "a", "text": "1"}, {"id": "b", "text": "2"}])
    assert [d.doc_id for d in docs] == ["a", "b"]


def test_read_jsonl_skips_blank_lines() -> None:
    docs = read_jsonl('{"id":"a","text":"1"}\n\n   \n{"id":"b","text":"2"}')
    assert [d.doc_id for d in docs] == ["a", "b"]


def test_read_jsonl_invalid_line_reports_number() -> None:
    with pytest.raises(IngestError) as exc:
        read_jsonl('{"text":"ok"}\nnot json')
    assert exc.value.context["line"] == 2


def test_read_jsonl_non_object_line_raises() -> None:
    with pytest.raises(IngestError):
        read_jsonl("[1, 2, 3]")


def test_read_records_custom_normalizer() -> None:
    docs = read_records([{"text": "x" * 10}], normalizer=SchemaNormalizer(max_chars=4))
    assert docs[0].text == "xxxx"
