from __future__ import annotations

from meridian_common.errors import MeridianError, ValidationError
from meridian_datapipe.errors import (
    ContaminationError,
    DedupError,
    IngestError,
    QualityError,
    ShardError,
    TokenizeError,
)


def test_all_datapipe_errors_are_meridian_errors() -> None:
    for cls in (IngestError, TokenizeError, DedupError, ContaminationError, ShardError, QualityError):
        assert issubclass(cls, MeridianError)


def test_ingest_error_is_a_validation_error() -> None:
    assert issubclass(IngestError, ValidationError)
    err = IngestError("bad", code="datapipe.ingest", line=3)
    assert err.context["line"] == 3


def test_error_codes_are_namespaced() -> None:
    assert TokenizeError.code == "datapipe.tokenize"
    assert DedupError.code == "datapipe.dedup"
    assert ContaminationError.code == "datapipe.contamination"
    assert ShardError.code == "datapipe.shard"
    assert QualityError.code == "datapipe.quality"


def test_error_serializes_to_dict() -> None:
    err = ShardError("nope", code="datapipe.shard", doc_id="d1")
    payload = err.to_dict()
    assert payload["code"] == "datapipe.shard"
    assert payload["context"]["doc_id"] == "d1"
