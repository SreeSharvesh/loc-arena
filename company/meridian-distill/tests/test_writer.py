from __future__ import annotations

import pytest

from meridian_datapipe.types import Document
from meridian_distill.errors import WriterError
from meridian_distill.prompts import PromptTemplate
from meridian_distill.teacher.response import TeacherResponse
from meridian_distill.writer import DistillationWriter, DistillRecord


def _record(doc_id: str) -> DistillRecord:
    doc = Document(doc_id, f"body for {doc_id}", source="corpus")
    prompt = PromptTemplate().render(doc)
    response = TeacherResponse(
        request_key=f"k-{doc_id}",
        prompt=prompt.token_ids,
        tokens=(1, 2, 3),
        features=(0.5, 0.5),
    )
    return DistillationWriter.record(doc, prompt, response)


def test_record_captures_all_fields() -> None:
    rec = _record("d1")
    assert rec.doc_id == "d1"
    assert rec.source == "corpus"
    assert rec.teacher_tokens == (1, 2, 3)
    assert rec.features == (0.5, 0.5)
    assert rec.prompt_tokens


def test_write_is_deterministic() -> None:
    records = [_record("d1"), _record("d2")]
    a = DistillationWriter().write(records)
    b = DistillationWriter().write(records)
    assert a.checksum == b.checksum
    assert a.num_records == 2
    assert len(a) == 2


def test_checksum_changes_with_records() -> None:
    one = DistillationWriter().write([_record("d1")])
    two = DistillationWriter().write([_record("d1"), _record("d2")])
    assert one.checksum != two.checksum


def test_duplicate_record_id_is_rejected() -> None:
    with pytest.raises(WriterError):
        DistillationWriter().write([_record("d1"), _record("d1")])


def test_dataset_to_dict_round_trips_fields() -> None:
    dataset = DistillationWriter().write([_record("d1")])
    payload = dataset.to_dict()
    assert payload["num_records"] == 1
    assert payload["records"][0]["doc_id"] == "d1"
    assert payload["checksum"] == dataset.checksum
