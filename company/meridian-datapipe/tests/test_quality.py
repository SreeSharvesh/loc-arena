from __future__ import annotations

import pytest

from meridian_datapipe.errors import QualityError
from meridian_datapipe.quality import QualityGate, QualityReport, alpha_ratio
from meridian_datapipe.types import Document


def test_alpha_ratio_bounds() -> None:
    assert alpha_ratio("abc") == 1.0
    assert alpha_ratio("") == 1.0
    assert alpha_ratio("ab12") == pytest.approx(0.5)
    assert alpha_ratio("   ") == 1.0


def test_gate_rejects_contradictory_length_bounds() -> None:
    with pytest.raises(QualityError):
        QualityGate(min_length=10, max_length=5)


def test_gate_rejects_bad_alpha_ratio() -> None:
    with pytest.raises(QualityError):
        QualityGate(min_alpha_ratio=1.5)


def test_gate_rejects_bad_duplicate_rate() -> None:
    with pytest.raises(QualityError):
        QualityGate(max_duplicate_rate=2.0)


def test_check_document_passes_clean_text() -> None:
    gate = QualityGate(min_length=3, max_length=100, min_alpha_ratio=0.5)
    report = gate.check_document(Document("a", "hello world"))
    assert report.passed and report.reasons == ()


def test_check_document_flags_too_short() -> None:
    gate = QualityGate(min_length=5)
    report = gate.check_document(Document("a", "hi"))
    assert not report.passed
    assert report.failing_ids == ("a",)


def test_check_document_flags_too_long() -> None:
    gate = QualityGate(max_length=3)
    report = gate.check_document(Document("a", "toolong"))
    assert not report.passed


def test_check_document_flags_low_alpha() -> None:
    gate = QualityGate(min_alpha_ratio=0.5)
    report = gate.check_document(Document("a", "1234$$"))
    assert not report.passed


def test_evaluate_aggregates_document_reasons() -> None:
    gate = QualityGate(min_length=3, min_alpha_ratio=0.3)
    docs = [Document("a", "good text"), Document("b", "x"), Document("c", "$$$$")]
    report = gate.evaluate(docs)
    assert not report.passed
    assert set(report.failing_ids) == {"b", "c"}
    assert report.checked == 3


def test_evaluate_passes_clean_corpus() -> None:
    gate = QualityGate(min_length=1, min_alpha_ratio=0.2, max_duplicate_rate=1.0)
    report = gate.evaluate([Document("a", "alpha"), Document("b", "beta")])
    assert report.passed


def test_evaluate_flags_high_duplicate_rate() -> None:
    gate = QualityGate(min_length=1, max_duplicate_rate=0.2)
    docs = [Document("a", "same"), Document("b", "same"), Document("c", "same")]
    report = gate.evaluate(docs)
    assert not report.passed
    assert any("duplicate rate" in reason for reason in report.reasons)


def test_evaluate_empty_corpus_passes() -> None:
    report = QualityGate().evaluate([])
    assert report.passed and report.checked == 0


def test_quality_report_failure_count() -> None:
    report = QualityReport(passed=False, reasons=("x",), checked=2, failing_ids=("a", "b"))
    assert report.failure_count == 2
